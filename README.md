# BeamTV — DLNA Media Server

Servidor DLNA / UPnP AV de escritorio ultraligero, diseñado específicamente para televisores **Samsung Smart TV (QLED / Tizen / Orsay)** en Windows.

## Características

- **100% Portable (`BeamTV.exe`)**: Ejecutable autónomo para Windows que no requiere instalar Python ni dependencias externas.
- **Descubrimiento automático (SSDP)**: Anuncio multicast UDP (`239.255.255.250:1900`) con conexión (`ssdp:alive`) y desconexión limpia (`ssdp:byebye`) al cerrar la ventana.
- **Exploración en tiempo real (SOAP / DIDL-Lite)**: Sin indexaciones lentas al iniciar. Cualquier archivo o carpeta nueva aparece al instante en la TV.
- **Streaming por bloques (`HTTP 206 Partial Content`)**: Permite adelantar, retroceder y pausar películas de gran tamaño consumiendo un mínimo de memoria RAM.
- **Soporte nativo de subtítulos Samsung (`.srt`)**: Inyecta automáticamente las cabeceras y etiquetas `sec:CaptionInfoEx` cuando existe un archivo `.srt` junto al vídeo.
- **Interfaz oscura nativa en Windows**: Gestión visual de carpetas compartidas, persistencia automática en `config.json` y registro de actividad en tiempo real.

## Estructura del Proyecto

```text
BeamTV/
├── assets/
│   ├── icon.ico            # Icono de ventana, ejecutable y barra de tareas
│   └── icon.png            # Icono de cabecera y presentación DLNA en la TV
├── src/
│   ├── __init__.py
│   ├── app.py              # Interfaz gráfica de escritorio (Tkinter)
│   └── dlna_server.py      # Motor SSDP, UPnP/SOAP y Streaming HTTP 206
├── .gitignore
├── BeamTV.exe              # Ejecutable autónomo portable para cualquier PC Windows
├── build_exe.bat           # Script para recompilar BeamTV.exe desde el código fuente
├── config.example.json     # Plantilla de configuración
├── main.py                 # Punto de entrada en Python
└── README.md
```

## Uso Rápido

1. Ejecuta **`BeamTV.exe`** con doble clic (en cualquier PC con Windows, sin necesidad de instalar Python).
2. Pulsa **`+ Añadir Carpeta`** para seleccionar las carpetas que deseas compartir con tu TV.
3. Abre **Fuentes (Sources)** en tu TV Samsung y selecciona **BeamTV**.
