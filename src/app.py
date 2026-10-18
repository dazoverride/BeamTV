"""
Aplicación de escritorio para el Servidor DLNA (Samsung TV).
Interfaz oscura pulida con icono personalizado, tabla de carpetas y registro en color.
"""

import ctypes
import datetime
import json
import os
import queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import uuid

try:
    from src.dlna_server import DLNAServer, get_local_ip
except ImportError:
    from dlna_server import DLNAServer, get_local_ip

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.json"
ICON_ICO = PROJECT_ROOT / "assets" / "icon.ico"
ICON_PNG = PROJECT_ROOT / "assets" / "icon.png"


def setup_windows_integration():
    """Activa High-DPI y asigna un AppUserModelID propio para mostrar el icono en la barra de tareas."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dani.BeamTV.DLNAServer.1")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def apply_dark_titlebar(window: tk.Tk):
    """Activa la barra de título oscura nativa de Windows 10/11."""
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        value = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 11 / Win10 20H1+)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        # 19 = respaldo para compilaciones anteriores de Windows 10
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


class HoverButton(tk.Button):
    """Botón plano con efecto hover suave."""

    def __init__(self, master, bg_normal, bg_hover, fg_normal="#cdd6f4", **kwargs):
        super().__init__(
            master,
            bg=bg_normal,
            fg=fg_normal,
            activebackground=bg_hover,
            activeforeground=fg_normal,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            **kwargs,
        )
        self.bg_normal = bg_normal
        self.bg_hover = bg_hover
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def set_colors(self, bg_normal: str, bg_hover: str, fg_normal: str = None):
        self.bg_normal = bg_normal
        self.bg_hover = bg_hover
        self.config(bg=bg_normal, activebackground=bg_hover)
        if fg_normal:
            self.config(fg=fg_normal, activeforeground=fg_normal)

    def _on_enter(self, _event):
        self.config(bg=self.bg_hover)

    def _on_leave(self, _event):
        self.config(bg=self.bg_normal)


class DLNADesktopApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("BeamTV — DLNA Media Server")
        self.root.geometry("740x580")
        self.root.minsize(660, 500)
        self.root.configure(bg="#141622")

        self._set_window_icon()
        apply_dark_titlebar(self.root)

        self.log_queue = queue.Queue()
        self.config = self.load_config()

        self.server = DLNAServer(
            friendly_name=self.config["friendly_name"],
            folders=self.config["folders"],
            port=self.config.get("port", 8200),
            device_uuid=self.config["device_uuid"],
            log_callback=self.enqueue_log,
        )

        self._configure_ttk_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.root.after(100, self._poll_logs)

        if self.config.get("auto_start", True):
            self.root.after(200, self.start_server)
        else:
            self.append_log("[INFO] Listo. Pulsa 'Iniciar Servidor' para activar la transmisión.")

    def _set_window_icon(self):
        self.header_icon_img = None
        try:
            if ICON_ICO.exists():
                self.root.iconbitmap(default=str(ICON_ICO))
        except Exception:
            pass
        try:
            if ICON_PNG.exists():
                full_png = tk.PhotoImage(file=str(ICON_PNG))
                self.root.iconphoto(True, full_png)
                # Subsample 128x128 -> 32x32 para el icono interno de la cabecera
                self.header_icon_img = full_png.subsample(3, 3)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Persistencia (config.json)
    # -------------------------------------------------------------------------
    def load_config(self) -> dict:
        default_cfg = {
            "friendly_name": "BeamTV",
            "folders": [],
            "port": 8200,
            "auto_start": True,
            "device_uuid": str(uuid.uuid4()),
        }
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                default_cfg.update(data)
            except Exception:
                pass
        self.config = default_cfg
        self.save_config()
        return default_cfg

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.enqueue_log(f"[ERROR] No se pudo guardar config.json: {e}")

    # -------------------------------------------------------------------------
    # Estilos TTK (Tabla oscura y Scrollbar oscuro)
    # -------------------------------------------------------------------------
    def _configure_ttk_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "DLNA.Treeview",
            background="#11131e",
            foreground="#cdd6f4",
            fieldbackground="#11131e",
            borderwidth=0,
            rowheight=30,
            font=("Segoe UI", 9),
        )
        style.configure(
            "DLNA.Treeview.Heading",
            background="#1e2235",
            foreground="#89b4fa",
            relief="flat",
            borderwidth=0,
            padding=(10, 6),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "DLNA.Treeview",
            background=[("selected", "#2d3b5e")],
            foreground=[("selected", "#ffffff")],
        )
        style.map(
            "DLNA.Treeview.Heading",
            background=[("active", "#262b42")],
        )

        style.configure(
            "Dark.Vertical.TScrollbar",
            gripcount=0,
            background="#2c314c",
            darkcolor="#11131e",
            lightcolor="#11131e",
            troughcolor="#11131e",
            bordercolor="#11131e",
            arrowcolor="#7f849c",
            width=12,
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#454c73"), ("!disabled", "#2c314c")],
        )

    # -------------------------------------------------------------------------
    # Construcción de la Interfaz Gráfica
    # -------------------------------------------------------------------------
    def _build_ui(self):
        bg_card = "#1c1f30"
        border_card = "#2c314c"
        fg_text = "#e2e8f0"
        fg_muted = "#94a3b8"

        # ================= 1. CABECERA DE ESTADO =================
        top_frame = tk.Frame(
            self.root,
            bg=bg_card,
            padx=18,
            pady=14,
            highlightthickness=1,
            highlightbackground=border_card,
        )
        top_frame.pack(fill=tk.X, padx=16, pady=(16, 8))

        # Botones a la derecha (se empaquetan primero para garantizar su espacio)
        btn_right = tk.Frame(top_frame, bg=bg_card)
        btn_right.pack(side=tk.RIGHT, padx=(12, 0))

        self.toggle_btn = HoverButton(
            btn_right,
            text="▶  Iniciar Servidor",
            font=("Segoe UI", 10, "bold"),
            bg_normal="#a6e3a1",
            bg_hover="#8bd586",
            fg_normal="#11131e",
            padx=18,
            pady=8,
            command=self.toggle_server,
        )
        self.toggle_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.announce_btn = HoverButton(
            btn_right,
            text="↻  Refrescar TV",
            font=("Segoe UI", 9, "bold"),
            bg_normal="#2b3048",
            bg_hover="#39405f",
            fg_normal=fg_text,
            padx=14,
            pady=8,
            command=self.manual_announce,
        )
        self.announce_btn.pack(side=tk.LEFT)

        # Icono de aplicación a la izquierda
        if self.header_icon_img:
            icon_lbl = tk.Label(top_frame, image=self.header_icon_img, bg=bg_card)
            icon_lbl.pack(side=tk.LEFT, padx=(0, 14))

        # Bloque de estado central
        status_left = tk.Frame(top_frame, bg=bg_card)
        status_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        status_row = tk.Frame(status_left, bg=bg_card)
        status_row.pack(fill=tk.X, anchor="w")

        # Indicador LED dibujado con Canvas (nítido, sin emojis rotos)
        self.led_canvas = tk.Canvas(
            status_row,
            width=18,
            height=18,
            bg=bg_card,
            highlightthickness=0,
        )
        self.led_canvas.pack(side=tk.LEFT, pady=(2, 0))
        self._draw_led(active=False)

        self.status_label = tk.Label(
            status_row,
            text="SERVER OFF",
            font=("Segoe UI", 13, "bold"),
            bg=bg_card,
            fg="#f38ba8",
            anchor="w",
        )
        self.status_label.pack(side=tk.LEFT, padx=(7, 0))

        self.ip_label = tk.Label(
            status_left,
            text=self._format_substatus(),
            font=("Segoe UI", 9),
            bg=bg_card,
            fg=fg_muted,
            anchor="w",
        )
        self.ip_label.pack(fill=tk.X, pady=(3, 0))

        # ================= 2. BARRA DE CONFIGURACIÓN =================
        cfg_frame = tk.Frame(
            self.root,
            bg=bg_card,
            padx=18,
            pady=10,
            highlightthickness=1,
            highlightbackground=border_card,
        )
        cfg_frame.pack(fill=tk.X, padx=16, pady=6)

        tk.Label(
            cfg_frame,
            text="Nombre en TV:",
            font=("Segoe UI", 9, "bold"),
            bg=bg_card,
            fg=fg_text,
        ).pack(side=tk.LEFT)

        self.name_var = tk.StringVar(value=self.config["friendly_name"])
        entry_wrap = tk.Frame(cfg_frame, bg="#11131e", highlightthickness=1, highlightbackground="#333956")
        entry_wrap.pack(side=tk.LEFT, padx=(10, 8))

        self.name_entry = tk.Entry(
            entry_wrap,
            textvariable=self.name_var,
            font=("Segoe UI", 10),
            bg="#11131e",
            fg="#ffffff",
            insertbackground="#89b4fa",
            relief=tk.FLAT,
            width=22,
        )
        self.name_entry.pack(padx=8, pady=4)
        self.name_entry.bind("<FocusOut>", lambda _e: self.on_name_changed())
        self.name_entry.bind("<Return>", lambda _e: self.on_name_changed())

        HoverButton(
            cfg_frame,
            text="Guardar",
            font=("Segoe UI", 9),
            bg_normal="#2b3048",
            bg_hover="#39405f",
            fg_normal=fg_text,
            padx=12,
            pady=4,
            command=self.on_name_changed,
        ).pack(side=tk.LEFT)

        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start", True))
        auto_chk = tk.Checkbutton(
            cfg_frame,
            text="Iniciar automáticamente al abrir",
            variable=self.auto_start_var,
            font=("Segoe UI", 9),
            bg=bg_card,
            fg=fg_muted,
            selectcolor="#11131e",
            activebackground=bg_card,
            activeforeground=fg_text,
            cursor="hand2",
            command=self.on_auto_start_changed,
        )
        auto_chk.pack(side=tk.RIGHT)

        # ================= 3. REGISTRO DE ACTIVIDAD (Anclado abajo siempre visible) =================
        log_frame = tk.Frame(
            self.root,
            bg=bg_card,
            padx=18,
            pady=10,
            highlightthickness=1,
            highlightbackground=border_card,
        )
        log_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(6, 16))

        log_header = tk.Frame(log_frame, bg=bg_card)
        log_header.pack(fill=tk.X, pady=(0, 6))

        tk.Label(
            log_header,
            text="Registro de Actividad",
            font=("Segoe UI", 9, "bold"),
            bg=bg_card,
            fg=fg_muted,
            anchor="w",
        ).pack(side=tk.LEFT)

        HoverButton(
            log_header,
            text="Limpiar",
            font=("Segoe UI", 8),
            bg_normal="#23273b",
            bg_hover="#333956",
            fg_normal=fg_muted,
            padx=8,
            pady=2,
            command=self.clear_logs,
        ).pack(side=tk.RIGHT)

        self.log_text = tk.Text(
            log_frame,
            height=5,
            font=("Consolas", 9),
            bg="#0e101a",
            fg="#cdd6f4",
            padx=10,
            pady=8,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground="#262b42",
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.X)

        # ================= 4. TABLA DE CARPETAS COMPARTIDAS (Rellena el centro) =================
        folders_frame = tk.Frame(
            self.root,
            bg=bg_card,
            padx=18,
            pady=12,
            highlightthickness=1,
            highlightbackground=border_card,
        )
        folders_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=16, pady=6)

        header_folders = tk.Frame(folders_frame, bg=bg_card)
        header_folders.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            header_folders,
            text="Carpetas Compartidas",
            font=("Segoe UI", 10, "bold"),
            bg=bg_card,
            fg=fg_text,
        ).pack(side=tk.LEFT)

        btn_bar = tk.Frame(header_folders, bg=bg_card)
        btn_bar.pack(side=tk.RIGHT)

        HoverButton(
            btn_bar,
            text="+  Añadir Carpeta",
            font=("Segoe UI", 9, "bold"),
            bg_normal="#89b4fa",
            bg_hover="#74a8fc",
            fg_normal="#11131e",
            padx=14,
            pady=5,
            command=self.add_folder,
        ).pack(side=tk.LEFT, padx=(0, 6))

        HoverButton(
            btn_bar,
            text="−  Quitar",
            font=("Segoe UI", 9),
            bg_normal="#2b3048",
            bg_hover="#f38ba8",
            fg_normal=fg_text,
            padx=12,
            pady=5,
            command=self.remove_folder,
        ).pack(side=tk.LEFT, padx=(0, 6))

        HoverButton(
            btn_bar,
            text="Abrir Carpeta",
            font=("Segoe UI", 9),
            bg_normal="#2b3048",
            bg_hover="#39405f",
            fg_normal=fg_text,
            padx=12,
            pady=5,
            command=self.open_selected_folder,
        ).pack(side=tk.LEFT)

        tree_container = tk.Frame(folders_frame, bg="#11131e", highlightthickness=1, highlightbackground="#262b42")
        tree_container.pack(fill=tk.BOTH, expand=True)

        columns = ("name", "path", "status")
        self.folders_tree = ttk.Treeview(
            tree_container,
            columns=columns,
            show="headings",
            style="DLNA.Treeview",
            selectmode="browse",
            height=4,
        )
        self.folders_tree.heading("name", text="Nombre", anchor="w")
        self.folders_tree.heading("path", text="Ruta en Disco", anchor="w")
        self.folders_tree.heading("status", text="Estado", anchor="center")

        self.folders_tree.column("name", width=160, minwidth=110, anchor="w", stretch=False)
        self.folders_tree.column("path", width=390, minwidth=220, anchor="w", stretch=True)
        self.folders_tree.column("status", width=95, minwidth=80, anchor="center", stretch=False)

        scrollbar = ttk.Scrollbar(
            tree_container,
            orient=tk.VERTICAL,
            command=self.folders_tree.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.folders_tree.configure(yscrollcommand=scrollbar.set)

        self.folders_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Doble clic para abrir la carpeta en el Explorador de Windows
        self.folders_tree.bind("<Double-1>", lambda _e: self.open_selected_folder())
        self.folders_tree.bind("<Delete>", lambda _e: self.remove_folder())

        self._refresh_folders_tree()

        # Etiquetas de colores para el log
        self.log_text.tag_config("time", foreground="#6c7086")
        self.log_text.tag_config("on", foreground="#a6e3a1")
        self.log_text.tag_config("tv", foreground="#89dceb")
        self.log_text.tag_config("play", foreground="#f9e2af")
        self.log_text.tag_config("dir", foreground="#89b4fa")
        self.log_text.tag_config("off", foreground="#f38ba8")
        self.log_text.tag_config("info", foreground="#cdd6f4")

    def _draw_led(self, active: bool):
        self.led_canvas.delete("all")
        if active:
            # Halo exterior verde suave + núcleo brillante
            self.led_canvas.create_oval(1, 1, 17, 17, fill="#244735", outline="")
            self.led_canvas.create_oval(4, 4, 14, 14, fill="#a6e3a1", outline="")
        else:
            # Halo exterior rojo suave + núcleo rojo
            self.led_canvas.create_oval(1, 1, 17, 17, fill="#472430", outline="")
            self.led_canvas.create_oval(4, 4, 14, 14, fill="#f38ba8", outline="")

    def _format_substatus(self) -> str:
        ip = self.server.local_ip or get_local_ip()
        count = len(self.config.get("folders", []))
        return f"IP Local: {ip}   •   Puerto: {self.server.port}   •   Carpetas: {count}"

    # -------------------------------------------------------------------------
    # Gestión de Carpetas y Configuración
    # -------------------------------------------------------------------------
    def _refresh_folders_tree(self):
        for item in self.folders_tree.get_children():
            self.folders_tree.delete(item)

        for idx, folder in enumerate(self.config["folders"]):
            p = Path(folder)
            name = p.name or str(p)
            exists = os.path.isdir(folder)
            status = "Activa" if exists else "No encontrada"
            self.folders_tree.insert("", tk.END, iid=str(idx), values=(f"  {name}", folder, status))

        if hasattr(self, "ip_label"):
            self.ip_label.config(text=self._format_substatus())

    def add_folder(self):
        selected = filedialog.askdirectory(title="Selecciona una carpeta para compartir en el servidor DLNA")
        if not selected:
            return
        norm_path = str(Path(selected).resolve())
        if norm_path in self.config["folders"]:
            messagebox.showinfo("Carpeta duplicada", "Esa carpeta ya forma parte del servidor.")
            return

        self.config["folders"].append(norm_path)
        self.save_config()
        self._refresh_folders_tree()
        self.server.update_config(self.config["friendly_name"], self.config["folders"])
        self.append_log(f"[INFO] Carpeta añadida: {norm_path}")

    def remove_folder(self):
        sel = self.folders_tree.selection()
        if not sel:
            messagebox.showwarning("Sin selección", "Selecciona primero una carpeta de la tabla para quitarla.")
            return
        idx = int(sel[0])
        removed = self.config["folders"].pop(idx)
        self.save_config()
        self._refresh_folders_tree()
        self.server.update_config(self.config["friendly_name"], self.config["folders"])
        self.append_log(f"[INFO] Carpeta quitada: {removed}")

    def open_selected_folder(self):
        sel = self.folders_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        folder = self.config["folders"][idx]
        if os.path.isdir(folder):
            os.startfile(folder)

    def on_name_changed(self):
        new_name = self.name_var.get().strip() or "BeamTV"
        self.name_var.set(new_name)
        if new_name != self.config["friendly_name"]:
            self.config["friendly_name"] = new_name
            self.save_config()
            self.server.update_config(self.config["friendly_name"], self.config["folders"])
            self.append_log(f"[INFO] Nombre en TV cambiado a: '{new_name}'")

    def on_auto_start_changed(self):
        self.config["auto_start"] = bool(self.auto_start_var.get())
        self.save_config()

    # -------------------------------------------------------------------------
    # Control del Servidor DLNA
    # -------------------------------------------------------------------------
    def toggle_server(self):
        if self.server.running:
            self.stop_server()
        else:
            self.start_server()

    def start_server(self):
        self.on_name_changed()
        self.server.update_config(self.config["friendly_name"], self.config["folders"])
        if self.server.start():
            self._draw_led(active=True)
            self.status_label.config(text="SERVER ON", fg="#a6e3a1")
            self.ip_label.config(text=self._format_substatus())
            self.toggle_btn.config(text="■  Detener Servidor")
            self.toggle_btn.set_colors(bg_normal="#f38ba8", bg_hover="#e57393", fg_normal="#11131e")

            if not self.config["folders"]:
                self.append_log("[INFO] Pulsa '+ Añadir Carpeta' para agregar contenido al servidor.")

    def stop_server(self):
        self.server.stop()
        self._draw_led(active=False)
        self.status_label.config(text="SERVER OFF", fg="#f38ba8")
        self.toggle_btn.config(text="▶  Iniciar Servidor")
        self.toggle_btn.set_colors(bg_normal="#a6e3a1", bg_hover="#8bd586", fg_normal="#11131e")

    def manual_announce(self):
        if not self.server.running:
            messagebox.showinfo("Servidor detenido", "Inicia primero el servidor para refrescar la señal en la TV.")
            return
        self.server.update_config(self.config["friendly_name"], self.config["folders"])
        self.append_log("[ON] Señal de anuncio SSDP enviada a la red local.")

    # -------------------------------------------------------------------------
    # Registro de Actividad con Colores y Cierre Limpio
    # -------------------------------------------------------------------------
    def enqueue_log(self, msg: str):
        self.log_queue.put(msg)

    def clear_logs(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)

    def append_log(self, msg: str):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        tag = "info"
        if msg.startswith("[ON]"):
            tag = "on"
        elif msg.startswith("[TV]"):
            tag = "tv"
        elif msg.startswith("[PLAY]"):
            tag = "play"
        elif msg.startswith("[DIR]"):
            tag = "dir"
        elif msg.startswith("[OFF]") or msg.startswith("[ERROR]"):
            tag = "off"

        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{now}] ", "time")
        self.log_text.insert(tk.END, f"{msg}\n", tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_logs(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.append_log(msg)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_logs)

    def on_close(self):
        if self.server.running:
            self.server.stop()
        self.root.destroy()


def run_app():
    setup_windows_integration()
    root_win = tk.Tk()
    DLNADesktopApp(root_win)
    root_win.mainloop()


if __name__ == "__main__":
    run_app()
