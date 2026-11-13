# Bahía Alerta: nivel de los canales de Bahía Blanca

Mira las cámaras públicas del municipio en YouTube y estima cada 5 minutos cuánto subió el agua de cada canal. Una página web muestra el nivel, el video en vivo, la última captura analizada y el viento y la lluvia de la ciudad.

Cámaras incluidas:

| Solapa | Video |
|---|---|
| Sixto Laspiur y Av. Buenos Aires | `DXpoL-oLaKo` |
| Parque de Mayo | `HCNs0AIW1mU` |
| Casanova y Fuerte Argentino | `R5AjH5tHUvo` |
| Panamá y 1° de Marzo | `UG5Xw5Ph9ug` |

> **Aviso:** depende de servicios externos (transmisiones de YouTube, Open-Meteo, Servicio de Hidrografía Naval y Google Noticias). Si alguno deja de funcionar, cambia o se demora, la información puede estar desactualizada, incompleta o ser incorrecta. Es una herramienta experimental, no un sistema oficial de alerta.

## Cómo mide

- Por cada cámara toma 5 fotogramas (uno por segundo) y usa la mediana, que borra autos y ruido.
- Compara la textura del canal sobre 4 cortes transversales contra una imagen de referencia de la misma hora del día.
- Solo cuenta el agua que **avanza desde la orilla de base hacia lo seco** (cada cámara tiene marcado dónde está el agua normalmente, en `"agua"` de `CAMARAS`). Los cambios dentro del agua (reflejos del cielo, plantas flotantes) o lejos de ella (sombras, gente) se ignoran.
- Antes de comparar, alinea la captura con la referencia para corregir pequeños movimientos de la cámara (`bases/` guarda la imagen sobre la que se marcaron las líneas).
- Una subida se informa cuando la ven dos mediciones seguidas. Si la imagen entera no se parece a la referencia (un camión delante, la cámara movida), se marca «imagen no confiable» y no se mide.
- **0 %** es el nivel de base (el agua que corre siempre); **100 %** es el canal cubierto de borde a borde.
- Estados: Normal, Subiendo (≥ 15 %), Alto (≥ 40 %), Crítico (≥ 70 %).
- La primera vez que pasa por cada hora del día toma esa captura como referencia, así que las primeras 24 horas son de calibración. Si llueve fuerte durante ese día, recalibrar después con el canal normal.
- El clima viene de [Open-Meteo](https://open-meteo.com/): es un modelo meteorológico, no un pluviómetro.

## Correrlo

Requiere Docker con compose y salida a internet hacia YouTube y `api.open-meteo.com`.

```bash
docker compose up -d --build
```

La página queda en `http://localhost:8097`. Los datos (referencias, historial y capturas de los últimos 7 días) se guardan en `datos/`.

Variables de entorno (en `docker-compose.yml`):

| Variable | Para qué | Por defecto |
|---|---|---|
| `INTERVALO_MIN` | minutos entre rondas de medición | `5` |
| `PREFIJO` | ruta pública si va detrás de un proxy, por ejemplo `/alerta` | vacío |
| `CAMARAS` | cámaras a vigilar, separadas por coma | todas |
| `TZ` | zona horaria (las referencias son por hora local) | `America/Argentina/Buenos_Aires` |

`docker-compose.bishop.yml` es la variante publicada bajo `/alerta` detrás de Nginx Proxy Manager, en la red `proxy-net`.

## Avisos por Telegram (opcional)

La página funciona sola. Si además querés avisos por Telegram, copiá `.env.example` como `.env` y completá:

| Variable | Para qué |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot que da @BotFather. Sin esto, Telegram queda desactivado y la página anda igual. |
| `TELEGRAM_CHAT_ID` | Tu chat. Si lo dejás vacío, se registra el primer chat privado que le escriba al bot; los demás se ignoran. |
| `TELEGRAM_UMBRALES` | Niveles de un canal que disparan un aviso al subir, separados por coma (por defecto `15,40,70`). |
| `TELEGRAM_CANAL` | Opcional y apagado por defecto: canal público (por ejemplo `@bahiaalerta`) donde el bot publica los avisos importantes. El bot tiene que ser administrador del canal. |

El `.env` está en `.gitignore`: nunca lo subas. Un mismo bot no puede correr en dos servidores a la vez.

Comandos del bot: `/estado`, `/camaras`, `/foto <cámara>`.

## Panel de administración (opcional)

En `/admin` hay un panel con clave para cambiar la configuración sin tocar el código: nombre, video y estado de cada cámara, las líneas de medición (se arrastran sobre la imagen, junto con las orillas del agua normal), el intervalo de medición y los umbrales del semáforo. También permite recalibrar una cámara.

- La clave va en `ADMIN_CLAVE` del `.env` (8 caracteres o más). Si queda vacía, el panel está desactivado.
- Lo que se guarda queda en `datos/config.json` y pisa los valores por defecto del código.
- Tras 5 claves incorrectas desde la misma dirección, el ingreso se bloquea 15 minutos. Las sesiones duran 12 horas.

## Agregar una cámara

1. Capturar un fotograma de 1920×1080 del video.
2. Agregar la cámara en `CAMARAS` de `monitor.py` con 4 cortes que crucen el canal de orilla a orilla, de lejos a cerca.
3. Reconstruir el contenedor.

## Uso sin Docker

```bash
pip install opencv-python-headless numpy yt-dlp   # además hace falta ffmpeg
python app.py                     # web + medición
python monitor.py --una-vez       # una sola ronda por consola
python monitor.py --recalibrar --cam casanova
```
