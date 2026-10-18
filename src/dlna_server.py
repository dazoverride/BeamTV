"""
Motor principal del Servidor DLNA / UPnP AV optimizado para TVs Samsung.
Utiliza únicamente la librería estándar de Python.
"""

import base64
import html
import os
import re
import socket
import struct
import threading
import time
import uuid
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"

VIDEO_MIMES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".mts": "video/mp2t",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
}

AUDIO_MIMES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".wma": "audio/x-ms-wma",
}

IMAGE_MIMES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Icono PNG 1x1 válido en base64 para que la TV nunca reciba un 404 al pedir el icono
ICON_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAAAXNSR0IArs4c6QAAAARnQU1BAACx"
    "jwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAABLSURBVGhD7c4xAQAgDMCg+ed+N7hA4M5J01d3"
    "79nZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ+RsP6w4B8X8t4m0A"
    "AAAASUVORK5CYII="
)


def get_local_ip() -> str:
    """Detecta la dirección IP local activa en la red LAN/Wi-Fi."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def encode_object_id(root_idx: int, rel_path: str) -> str:
    """Codifica un índice de carpeta raíz y ruta relativa en un ObjectID seguro."""
    raw = f"{root_idx}:{rel_path}".encode("utf-8")
    return "obj_" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_object_id(object_id: str):
    """Decodifica un ObjectID en (root_idx, rel_path) o lanza ValueError."""
    if not object_id.startswith("obj_"):
        raise ValueError("Invalid ObjectID prefix")
    b64 = object_id[4:]
    padding = "=" * ((4 - len(b64) % 4) % 4)
    decoded = base64.urlsafe_b64decode(b64 + padding).decode("utf-8")
    idx_str, rel_path = decoded.split(":", 1)
    return int(idx_str), rel_path


class DLNAServer:
    """Servidor DLNA completo (SSDP + UPnP/SOAP + Streaming HTTP)."""

    def __init__(self, friendly_name: str, folders: list[str], port: int = 8200, device_uuid: str = None, log_callback=None):
        self.friendly_name = friendly_name or "Servidor DLNA"
        self.folders = list(folders)
        self.port = port
        self.device_uuid = device_uuid or str(uuid.uuid4())
        self.log_callback = log_callback
        self.local_ip = get_local_ip()
        self.system_update_id = 1

        self.running = False
        self.http_server = None
        self.http_thread = None
        self.ssdp_thread = None
        self.announce_thread = None
        self.ssdp_sock = None
        self._lock = threading.Lock()
        self._known_clients = set()

    def log(self, message: str):
        if self.log_callback:
            try:
                self.log_callback(message)
            except Exception:
                pass

    def update_config(self, friendly_name: str, folders: list[str]):
        """Actualiza carpetas o nombre en caliente sin necesidad de reiniciar."""
        with self._lock:
            self.friendly_name = friendly_name or "Servidor DLNA"
            self.folders = [f for f in folders if os.path.isdir(f)]
            self.system_update_id += 1
        if self.running:
            self._send_ssdp_notify(alive=True)

    def get_valid_folders(self) -> list[Path]:
        with self._lock:
            result = []
            for f in self.folders:
                p = Path(f)
                if p.exists() and p.is_dir():
                    result.append(p.resolve())
            return result

    def resolve_object_path(self, object_id: str) -> tuple[Path, str]:
        """
        Resuelve un ObjectID a (Path absoluto validado, parent_id).
        Previene cualquier acceso fuera de las carpetas autorizadas.
        """
        folders = self.get_valid_folders()
        root_idx, rel_path = decode_object_id(object_id)
        if root_idx < 0 or root_idx >= len(folders):
            raise FileNotFoundError("Índice de carpeta raíz fuera de rango")

        base_dir = folders[root_idx]
        if not rel_path or rel_path == ".":
            return base_dir, "0"

        target_path = (base_dir / rel_path).resolve()
        # Verificar que target_path esté dentro de base_dir
        try:
            target_path.relative_to(base_dir)
        except ValueError:
            raise PermissionError("Acceso fuera de la carpeta compartida denegado")

        if not target_path.exists():
            raise FileNotFoundError("El archivo o carpeta no existe")

        parent_rel = str(Path(rel_path).parent).replace("\\", "/")
        if parent_rel in ("", "."):
            parent_id = encode_object_id(root_idx, "")
        else:
            parent_id = encode_object_id(root_idx, parent_rel)

        return target_path, parent_id

    def find_subtitle_for_video(self, video_path: Path) -> Path | None:
        """Busca un archivo .srt con el mismo nombre junto al vídeo."""
        srt_direct = video_path.with_suffix(".srt")
        if srt_direct.exists() and srt_direct.is_file():
            return srt_direct
        # Buscar variantes como nombre.es.srt o nombre.spa.srt
        stem_lower = video_path.stem.lower()
        try:
            for item in video_path.parent.iterdir():
                if item.is_file() and item.suffix.lower() == ".srt":
                    if item.stem.lower().startswith(stem_lower):
                        return item
        except Exception:
            pass
        return None

    # -------------------------------------------------------------------------
    # Ciclo de vida del Servidor (Start / Stop)
    # -------------------------------------------------------------------------
    def start(self) -> bool:
        if self.running:
            return True

        self.local_ip = get_local_ip()
        server_ref = self

        class RequestHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format_str, *args):
                # Silenciar el log por consola por defecto de BaseHTTPRequestHandler
                pass

            def do_HEAD(self):
                self._handle_request(head_only=True)

            def do_GET(self):
                self._handle_request(head_only=False)

            def do_POST(self):
                server_ref.handle_soap_post(self)

            def do_SUBSCRIBE(self):
                server_ref.handle_subscribe(self)

            def do_UNSUBSCRIBE(self):
                server_ref.handle_unsubscribe(self)

            def _handle_request(self, head_only: bool):
                server_ref.handle_http_get(self, head_only=head_only)

        # Intentar abrir el puerto HTTP (si está ocupado, probar los siguientes)
        for candidate_port in range(self.port, self.port + 20):
            try:
                self.http_server = ThreadingHTTPServer(("0.0.0.0", candidate_port), RequestHandler)
                self.http_server.daemon_threads = True
                self.port = candidate_port
                break
            except OSError:
                continue
        else:
            self.log("[ERROR] No se pudo abrir un puerto HTTP disponible.")
            return False

        self.running = True
        self._known_clients.clear()

        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()

        self.ssdp_thread = threading.Thread(target=self._ssdp_listener_loop, daemon=True)
        self.ssdp_thread.start()

        self.announce_thread = threading.Thread(target=self._ssdp_announce_loop, daemon=True)
        self.announce_thread.start()

        self.log(f"[ON] Servidor activo en http://{self.local_ip}:{self.port} ({self.friendly_name})")
        return True

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.log("[OFF] Deteniendo servidor y desconectando de la TV...")

        try:
            self._send_ssdp_notify(alive=False)
        except Exception:
            pass

        if self.ssdp_sock:
            try:
                self.ssdp_sock.close()
            except Exception:
                pass
            self.ssdp_sock = None

        if self.http_server:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception:
                pass
            self.http_server = None

        self.log("[OFF] Servidor detenido correctamente.")

    # -------------------------------------------------------------------------
    # SSDP (UDP Multicast 1900)
    # -------------------------------------------------------------------------
    def _get_ssdp_targets(self) -> list[tuple[str, str]]:
        u = f"uuid:{self.device_uuid}"
        return [
            ("upnp:rootdevice", f"{u}::upnp:rootdevice"),
            (u, u),
            ("urn:schemas-upnp-org:device:MediaServer:1", f"{u}::urn:schemas-upnp-org:device:MediaServer:1"),
            ("urn:schemas-upnp-org:service:ContentDirectory:1", f"{u}::urn:schemas-upnp-org:service:ContentDirectory:1"),
            ("urn:schemas-upnp-org:service:ConnectionManager:1", f"{u}::urn:schemas-upnp-org:service:ConnectionManager:1"),
        ]

    def _send_ssdp_notify(self, alive: bool = True):
        location = f"http://{self.local_ip}:{self.port}/description.xml"
        nts = "ssdp:alive" if alive else "ssdp:byebye"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.local_ip))
                except Exception:
                    pass

                for nt, usn in self._get_ssdp_targets():
                    if alive:
                        msg = (
                            "NOTIFY * HTTP/1.1\r\n"
                            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                            "CACHE-CONTROL: max-age=1800\r\n"
                            f"LOCATION: {location}\r\n"
                            f"NT: {nt}\r\n"
                            f"NTS: {nts}\r\n"
                            "SERVER: Windows/10.0 UPnP/1.0 PythonDLNA/1.0\r\n"
                            f"USN: {usn}\r\n"
                            "\r\n"
                        )
                    else:
                        msg = (
                            "NOTIFY * HTTP/1.1\r\n"
                            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                            f"NT: {nt}\r\n"
                            f"NTS: {nts}\r\n"
                            f"USN: {usn}\r\n"
                            "\r\n"
                        )
                    sock.sendto(msg.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))
        except Exception:
            pass

    def _ssdp_announce_loop(self):
        # Anuncio inicial doble para detección inmediata en la TV Samsung
        self._send_ssdp_notify(alive=True)
        time.sleep(1.2)
        if self.running:
            self._send_ssdp_notify(alive=True)

        counter = 0
        while self.running:
            time.sleep(1.0)
            counter += 1
            if counter >= 25:
                counter = 0
                self._send_ssdp_notify(alive=True)

    def _ssdp_listener_loop(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", SSDP_PORT))

            # Unirse al grupo multicast en la interfaz local activa y en INADDR_ANY por respaldo
            try:
                mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(self.local_ip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except Exception:
                mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            sock.settimeout(1.0)
            self.ssdp_sock = sock
        except Exception as e:
            self.log(f"⚠️ Aviso UDP 1900: {e} (El anuncio activo sigue funcionando)")
            return

        while self.running:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception:
                break

            try:
                text = data.decode("utf-8", errors="ignore")
                if not text.startswith("M-SEARCH"):
                    continue

                headers = {}
                for line in text.split("\r\n")[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().upper()] = v.strip()

                st = headers.get("ST", "")
                if not st:
                    continue

                targets = self._get_ssdp_targets()
                matched = []
                if st == "ssdp:all":
                    matched = targets
                else:
                    for nt, usn in targets:
                        if st.lower() == nt.lower():
                            matched.append((nt, usn))

                if matched:
                    location = f"http://{self.local_ip}:{self.port}/description.xml"
                    now_str = formatdate(timeval=None, localtime=False, usegmt=True)
                    for nt, usn in matched:
                        resp = (
                            "HTTP/1.1 200 OK\r\n"
                            "CACHE-CONTROL: max-age=1800\r\n"
                            f"DATE: {now_str}\r\n"
                            "EXT:\r\n"
                            f"LOCATION: {location}\r\n"
                            "SERVER: Windows/10.0 UPnP/1.0 PythonDLNA/1.0\r\n"
                            f"ST: {nt}\r\n"
                            f"USN: {usn}\r\n"
                            "\r\n"
                        )
                        sock.sendto(resp.encode("utf-8"), addr)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Peticiones HTTP (Descripciones XML, Subtítulos y Streaming 206)
    # -------------------------------------------------------------------------
    def handle_subscribe(self, handler: BaseHTTPRequestHandler):
        sid = f"uuid:{uuid.uuid4()}"
        handler.send_response(200)
        handler.send_header("SID", sid)
        handler.send_header("TIMEOUT", "Second-1800")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def handle_unsubscribe(self, handler: BaseHTTPRequestHandler):
        handler.send_response(200)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def handle_http_get(self, handler: BaseHTTPRequestHandler, head_only: bool = False):
        path = unquote(handler.path.split("?", 1)[0])

        if path == "/description.xml":
            client_ip = handler.client_address[0]
            if client_ip not in self._known_clients and client_ip != "127.0.0.1":
                self._known_clients.add(client_ip)
                ua = handler.headers.get("USER-AGENT", "Dispositivo DLNA")
                self.log(f"[TV] Conectada ({client_ip}) - {ua}")
            xml_bytes = self._build_description_xml().encode("utf-8")
            self._send_bytes(handler, 200, "text/xml; charset=utf-8", xml_bytes, head_only)
            return

        if path == "/ContentDirectory.xml":
            xml_bytes = self._build_content_directory_scpd().encode("utf-8")
            self._send_bytes(handler, 200, "text/xml; charset=utf-8", xml_bytes, head_only)
            return

        if path == "/ConnectionManager.xml":
            xml_bytes = self._build_connection_manager_scpd().encode("utf-8")
            self._send_bytes(handler, 200, "text/xml; charset=utf-8", xml_bytes, head_only)
            return

        if path == "/icon.png":
            custom_icon = Path(__file__).resolve().parent.parent / "assets" / "icon.png"
            icon_bytes = custom_icon.read_bytes() if custom_icon.exists() else ICON_PNG_BYTES
            self._send_bytes(handler, 200, "image/png", icon_bytes, head_only)
            return

        if path.startswith("/sub/"):
            # Formato: /sub/<object_id>.srt
            obj_part = path[len("/sub/"):]
            if obj_part.endswith(".srt"):
                obj_part = obj_part[:-4]
            try:
                video_path, _ = self.resolve_object_path(obj_part)
                srt_path = self.find_subtitle_for_video(video_path)
                if not srt_path:
                    handler.send_error(404, "Subtitle not found")
                    return
                self._stream_file(handler, srt_path, "text/srt", head_only=head_only, is_media=False)
            except Exception:
                handler.send_error(404, "Subtitle not found")
            return

        if path.startswith("/media/"):
            # Formato: /media/<object_id>/<filename>
            parts = path.split("/")
            if len(parts) < 3:
                handler.send_error(404, "Invalid media path")
                return
            object_id = parts[2]
            try:
                file_path, _ = self.resolve_object_path(object_id)
                if not file_path.is_file():
                    handler.send_error(404, "File not found")
                    return
                ext = file_path.suffix.lower()
                mime = VIDEO_MIMES.get(ext) or AUDIO_MIMES.get(ext) or IMAGE_MIMES.get(ext) or "application/octet-stream"
                srt_path = self.find_subtitle_for_video(file_path) if ext in VIDEO_MIMES else None
                srt_url = f"http://{self.local_ip}:{self.port}/sub/{object_id}.srt" if srt_path else None
                self._stream_file(
                    handler,
                    file_path,
                    mime,
                    head_only=head_only,
                    is_media=(ext in VIDEO_MIMES or ext in AUDIO_MIMES),
                    subtitle_url=srt_url,
                )
            except Exception:
                handler.send_error(404, "File not found")
            return

        handler.send_error(404, "Not Found")

    def _send_bytes(self, handler: BaseHTTPRequestHandler, status: int, content_type: str, data: bytes, head_only: bool):
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            if not head_only:
                handler.wfile.write(data)
        except Exception:
            pass

    def _stream_file(
        self,
        handler: BaseHTTPRequestHandler,
        file_path: Path,
        mime: str,
        head_only: bool = False,
        is_media: bool = True,
        subtitle_url: str | None = None,
    ):
        file_size = file_path.stat().st_size
        range_header = handler.headers.get("Range")

        start = 0
        end = file_size - 1
        status = 200

        if range_header and range_header.startswith("bytes="):
            try:
                range_spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
                start_str, end_str = range_spec.split("-", 1)
                if start_str:
                    start = int(start_str)
                    if end_str:
                        end = min(int(end_str), file_size - 1)
                elif end_str:
                    # Sufijo de bytes (ej. bytes=-500)
                    start = max(0, file_size - int(end_str))
                if 0 <= start <= end < file_size:
                    status = 206
            except Exception:
                status = 200
                start = 0
                end = file_size - 1

        content_length = (end - start + 1) if file_size > 0 else 0

        try:
            handler.send_response(status)
            handler.send_header("Content-Type", mime)
            handler.send_header("Accept-Ranges", "bytes")
            if status == 206:
                handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            handler.send_header("Content-Length", str(content_length))
            handler.send_header("transferMode.dlna.org", "Streaming" if is_media else "Interactive")
            handler.send_header("contentFeatures.dlna.org", DLNA_FLAGS)
            if subtitle_url:
                handler.send_header("CaptionInfo.sec", subtitle_url)
            handler.send_header("Connection", "close")
            handler.end_headers()

            if head_only or content_length <= 0:
                return

            if start == 0:
                self.log(f"[PLAY] Reproduciendo en TV: {file_path.name}")

            chunk_size = 256 * 1024  # Bloques de 256 KB
            remaining = content_length
            with open(file_path, "rb") as f:
                f.seek(start)
                while remaining > 0 and self.running:
                    data = f.read(min(chunk_size, remaining))
                    if not data:
                        break
                    handler.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # Es normal cuando la TV Samsung adelanta, pausa o cierra una sonda de metadatos
            pass

    # -------------------------------------------------------------------------
    # SOAP Control (Browse, GetSystemUpdateID, Samsung X_GetFeatureList, etc.)
    # -------------------------------------------------------------------------
    def handle_soap_post(self, handler: BaseHTTPRequestHandler):
        content_length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(content_length).decode("utf-8", errors="ignore") if content_length > 0 else ""
        soap_action = handler.headers.get("SOAPACTION", "") or handler.headers.get("SOAPAction", "")

        if "Browse" in soap_action or "<u:Browse" in body or ":Browse" in body:
            resp_xml = self._soap_browse(body, handler.client_address[0])
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "GetSystemUpdateID" in soap_action or "GetSystemUpdateID" in body:
            resp_xml = self._soap_envelope(
                "u:GetSystemUpdateIDResponse",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
                f"<Id>{self.system_update_id}</Id>",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "GetSearchCapabilities" in soap_action or "GetSearchCapabilities" in body:
            resp_xml = self._soap_envelope(
                "u:GetSearchCapabilitiesResponse",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
                "<SearchCaps></SearchCaps>",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "GetSortCapabilities" in soap_action or "GetSortCapabilities" in body:
            resp_xml = self._soap_envelope(
                "u:GetSortCapabilitiesResponse",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
                "<SortCaps>dc:title</SortCaps>",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "X_GetFeatureList" in soap_action or "X_GetFeatureList" in body:
            features = html.escape(
                '<?xml version="1.0" encoding="utf-8"?>'
                '<Features xmlns="urn:schemas-upnp-org:av:avs" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xsi:schemaLocation="urn:schemas-upnp-org:av:avs http://www.upnp.org/schemas/av/avs.xsd">'
                "</Features>"
            )
            resp_xml = self._soap_envelope(
                "u:X_GetFeatureListResponse",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
                f"<FeatureList>{features}</FeatureList>",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "X_SetBookmark" in soap_action or "X_SetBookmark" in body:
            resp_xml = self._soap_envelope(
                "u:X_SetBookmarkResponse",
                "urn:schemas-upnp-org:service:ContentDirectory:1",
                "",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "GetProtocolInfo" in soap_action or "GetProtocolInfo" in body:
            sources = ",".join(
                [f"http-get:*:{m}:*" for m in list(VIDEO_MIMES.values()) + list(AUDIO_MIMES.values()) + list(IMAGE_MIMES.values())]
            )
            resp_xml = self._soap_envelope(
                "u:GetProtocolInfoResponse",
                "urn:schemas-upnp-org:service:ConnectionManager:1",
                f"<Source>{sources}</Source><Sink></Sink>",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        if "GetCurrentConnectionIDs" in soap_action or "GetCurrentConnectionIDs" in body:
            resp_xml = self._soap_envelope(
                "u:GetCurrentConnectionIDsResponse",
                "urn:schemas-upnp-org:service:ConnectionManager:1",
                "<ConnectionIDs>0</ConnectionIDs>",
            )
            self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)
            return

        # Respuesta genérica para cualquier otra acción SOAP
        resp_xml = self._soap_envelope(
            "u:BrowseResponse",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
            "<Result></Result><NumberReturned>0</NumberReturned><TotalMatches>0</TotalMatches><UpdateID>1</UpdateID>",
        )
        self._send_bytes(handler, 200, 'text/xml; charset="utf-8"', resp_xml.encode("utf-8"), head_only=False)

    @staticmethod
    def _extract_xml_tag(xml_text: str, tag_name: str, default: str = "") -> str:
        match = re.search(rf"<(?:\w+:)?{tag_name}[^>]*>(.*?)</(?:\w+:)?{tag_name}>", xml_text, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else default

    def _soap_browse(self, body: str, client_ip: str) -> str:
        object_id = self._extract_xml_tag(body, "ObjectID", "0")
        browse_flag = self._extract_xml_tag(body, "BrowseFlag", "BrowseDirectChildren")
        starting_index = int(self._extract_xml_tag(body, "StartingIndex", "0") or 0)
        requested_count = int(self._extract_xml_tag(body, "RequestedCount", "0") or 0)

        folders = self.get_valid_folders()

        # Si la TV pide categorías especiales Samsung (ej. "V_F", "A_F", "I_F") que no comienzan con obj_,
        # las redirigimos al contenedor raíz "0" para mostrar directamente nuestras carpetas.
        if object_id != "0" and not object_id.startswith("obj_"):
            object_id = "0"

        didl_items = []
        total_matches = 0
        number_returned = 0

        if browse_flag == "BrowseMetadata":
            if object_id == "0":
                didl_items.append(
                    self._didl_container(
                        obj_id="0",
                        parent_id="-1",
                        title=self.friendly_name,
                        child_count=len(folders),
                    )
                )
                total_matches = 1
                number_returned = 1
            else:
                try:
                    target_path, parent_id = self.resolve_object_path(object_id)
                    if target_path.is_dir():
                        children = self._list_directory_entries(target_path)
                        didl_items.append(
                            self._didl_container(
                                obj_id=object_id,
                                parent_id=parent_id,
                                title=target_path.name or str(target_path),
                                child_count=len(children),
                            )
                        )
                    else:
                        item_xml = self._didl_media_item(object_id, parent_id, target_path)
                        if item_xml:
                            didl_items.append(item_xml)
                    total_matches = len(didl_items)
                    number_returned = len(didl_items)
                except Exception:
                    pass
        else:
            # BrowseDirectChildren
            all_entries = []
            if object_id == "0":
                self.log(f"[DIR] TV ({client_ip}) explorando carpetas principales")
                for idx, folder_path in enumerate(folders):
                    cid = encode_object_id(idx, "")
                    children = self._list_directory_entries(folder_path)
                    # Mostrar el nombre de la carpeta; si hay varias con el mismo nombre, incluir ruta corta
                    title = folder_path.name or str(folder_path)
                    all_entries.append(
                        self._didl_container(
                            obj_id=cid,
                            parent_id="0",
                            title=title,
                            child_count=len(children),
                        )
                    )
            else:
                try:
                    target_path, _ = self.resolve_object_path(object_id)
                    root_idx, rel_path = decode_object_id(object_id)
                    if target_path.is_dir():
                        self.log(f"[DIR] TV explorando: {target_path.name}")
                        entries = self._list_directory_entries(target_path)
                        for entry in entries:
                            entry_rel = entry.name if not rel_path else f"{rel_path}/{entry.name}"
                            entry_id = encode_object_id(root_idx, entry_rel)
                            if entry.is_dir():
                                sub_children = self._list_directory_entries(entry)
                                all_entries.append(
                                    self._didl_container(
                                        obj_id=entry_id,
                                        parent_id=object_id,
                                        title=entry.name,
                                        child_count=len(sub_children),
                                    )
                                )
                            else:
                                item_xml = self._didl_media_item(entry_id, object_id, entry)
                                if item_xml:
                                    all_entries.append(item_xml)
                except Exception:
                    pass

            total_matches = len(all_entries)
            if requested_count > 0:
                sliced = all_entries[starting_index : starting_index + requested_count]
            else:
                sliced = all_entries[starting_index:]
            didl_items = sliced
            number_returned = len(didl_items)

        didl_xml = (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:sec="http://www.sec.co.kr/">'
            + "".join(didl_items)
            + "</DIDL-Lite>"
        )

        inner = (
            f"<Result>{html.escape(didl_xml)}</Result>"
            f"<NumberReturned>{number_returned}</NumberReturned>"
            f"<TotalMatches>{total_matches}</TotalMatches>"
            f"<UpdateID>{self.system_update_id}</UpdateID>"
        )
        return self._soap_envelope(
            "u:BrowseResponse",
            "urn:schemas-upnp-org:service:ContentDirectory:1",
            inner,
        )

    @staticmethod
    def _list_directory_entries(folder: Path) -> list[Path]:
        """Lista subcarpetas y archivos multimedia válidos ordenados alfabéticamente."""
        dirs = []
        files = []
        try:
            for item in folder.iterdir():
                if item.name.startswith(".") or item.name.startswith("$"):
                    continue
                if item.is_dir():
                    dirs.append(item)
                elif item.is_file():
                    ext = item.suffix.lower()
                    if ext in VIDEO_MIMES or ext in AUDIO_MIMES or ext in IMAGE_MIMES:
                        files.append(item)
        except Exception:
            pass
        dirs.sort(key=lambda p: p.name.lower())
        files.sort(key=lambda p: p.name.lower())
        return dirs + files

    @staticmethod
    def _didl_container(obj_id: str, parent_id: str, title: str, child_count: int) -> str:
        safe_title = html.escape(title)
        return (
            f'<container id="{obj_id}" parentID="{parent_id}" restricted="1" searchable="0" childCount="{child_count}">'
            f"<dc:title>{safe_title}</dc:title>"
            "<upnp:class>object.container.storageFolder</upnp:class>"
            "</container>"
        )

    def _didl_media_item(self, obj_id: str, parent_id: str, file_path: Path) -> str:
        ext = file_path.suffix.lower()
        if ext in VIDEO_MIMES:
            mime = VIDEO_MIMES[ext]
            upnp_class = "object.item.videoItem"
        elif ext in AUDIO_MIMES:
            mime = AUDIO_MIMES[ext]
            upnp_class = "object.item.audioItem.musicTrack"
        elif ext in IMAGE_MIMES:
            mime = IMAGE_MIMES[ext]
            upnp_class = "object.item.imageItem.photo"
        else:
            return ""

        try:
            size = file_path.stat().st_size
        except Exception:
            size = 0

        safe_title = html.escape(file_path.stem)
        # Nombre seguro en la URL para que la TV vea la extensión correcta
        safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", file_path.name)
        media_url = f"http://{self.local_ip}:{self.port}/media/{obj_id}/{safe_filename}"
        protocol_info = f"http-get:*:{mime}:{DLNA_FLAGS}"

        sub_tags = ""
        if ext in VIDEO_MIMES:
            srt_path = self.find_subtitle_for_video(file_path)
            if srt_path:
                sub_url = f"http://{self.local_ip}:{self.port}/sub/{obj_id}.srt"
                sub_tags = (
                    f'<sec:CaptionInfoEx sec:type="srt">{sub_url}</sec:CaptionInfoEx>'
                    f'<sec:CaptionInfo sec:type="srt">{sub_url}</sec:CaptionInfo>'
                    f'<res protocolInfo="http-get:*:text/srt:*">{sub_url}</res>'
                )

        return (
            f'<item id="{obj_id}" parentID="{parent_id}" restricted="1">'
            f"<dc:title>{safe_title}</dc:title>"
            f"<upnp:class>{upnp_class}</upnp:class>"
            f"{sub_tags}"
            f'<res size="{size}" protocolInfo="{protocol_info}">{media_url}</res>'
            "</item>"
        )

    @staticmethod
    def _soap_envelope(response_tag: str, service_urn: str, inner_xml: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            f'<{response_tag} xmlns:u="{service_urn}">'
            f"{inner_xml}"
            f"</{response_tag}>"
            "</s:Body>"
            "</s:Envelope>"
        )

    # -------------------------------------------------------------------------
    # Documentos XML UPnP (description.xml y SCPDs)
    # -------------------------------------------------------------------------
    def _build_description_xml(self) -> str:
        safe_name = html.escape(self.friendly_name)
        return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"
      xmlns:dlna="urn:schemas-dlna-org:device-1-0"
      xmlns:sec="http://www.sec.co.kr/dlna">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{safe_name}</friendlyName>
    <manufacturer>BeamTV</manufacturer>
    <manufacturerURL>http://www.samsung.com</manufacturerURL>
    <modelDescription>BeamTV DLNA Media Server</modelDescription>
    <modelName>Windows Media Connect Compatible</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>uuid:{self.device_uuid}</UDN>
    <dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
    <sec:ProductCap>smi,DCM10,getMediaInfo.sec,getCaptionInfo.sec</sec:ProductCap>
    <sec:X_ProductCap>smi,DCM10,getMediaInfo.sec,getCaptionInfo.sec</sec:X_ProductCap>
    <iconList>
      <icon>
        <mimetype>image/png</mimetype>
        <width>48</width>
        <height>48</height>
        <depth>24</depth>
        <url>/icon.png</url>
      </icon>
    </iconList>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/ContentDirectory.xml</SCPDURL>
        <controlURL>/ctl/ContentDirectory</controlURL>
        <eventSubURL>/evt/ContentDirectory</eventSubURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager.xml</SCPDURL>
        <controlURL>/ctl/ConnectionManager</controlURL>
        <eventSubURL>/evt/ConnectionManager</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""

    @staticmethod
    def _build_content_directory_scpd() -> str:
        return """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>Browse</name>
      <argumentList>
        <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
        <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
        <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
        <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
        <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
        <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
        <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
        <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetSystemUpdateID</name>
      <argumentList>
        <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetSearchCapabilities</name>
      <argumentList>
        <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetSortCapabilities</name>
      <argumentList>
        <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType>
      <allowedValueList><allowedValue>BrowseMetadata</allowedValue><allowedValue>BrowseDirectChildren</allowedValue></allowedValueList>
    </stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

    @staticmethod
    def _build_connection_manager_scpd() -> str:
        return """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>GetProtocolInfo</name>
      <argumentList>
        <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
        <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetCurrentConnectionIDs</name>
      <argumentList>
        <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""
