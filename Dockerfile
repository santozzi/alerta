FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir opencv-python-headless numpy yt-dlp

WORKDIR /app
COPY monitor.py app.py marea.py noticias.py smn.py telegram_bot.py ajustes.py admin.py ./
COPY web ./web
COPY bases ./bases

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "app.py"]
