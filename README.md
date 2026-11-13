# Bahía Alerta: nivel de los canales de Bahía Blanca

Mira las cámaras públicas del municipio en YouTube y estima cada 5 minutos cuánto subió el agua de cada canal. Una página web muestra el nivel, el video en vivo, la última captura analizada y el viento y la lluvia de la ciudad.

Cámaras incluidas:

| Solapa | Video |
|---|---|
| Sixto Laspiur y Av. Buenos Aires | `DXpoL-oLaKo` |
| Parque de Mayo | `HCNs0AIW1mU` |
| Casanova y Fuerte Argentino | `R5AjH5tHUvo` |
| Panamá y 1° de Marzo | `UG5Xw5Ph9ug` |

## Cómo mide

- Por cada cámara toma 5 fotogramas (uno por segundo) y usa la mediana, que borra autos y ruido.
- Compara la textura del canal sobre 4 cortes transversales contra una imagen de referencia de la misma hora del día.
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
