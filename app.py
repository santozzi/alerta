"""Servidor web + bucle de medicion de los canales (pensado para correr en Docker)."""
import csv
import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import monitor

INTERVALO = float(os.environ.get("INTERVALO_MIN", "5"))
PUERTO = int(os.environ.get("PUERTO", "8000"))
# Ruta publica cuando va detras de un proxy (ej. "/alerta"); vacio = raiz.
PREFIJO = os.environ.get("PREFIJO", "").rstrip("/")
# Camaras a vigilar (separadas por coma); vacio = todas.
CAMARAS = [c for c in os.environ.get("CAMARAS", "").split(",") if c in monitor.CAMARAS] or list(monitor.CAMARAS)
WEB = Path(__file__).resolve().parent / "web"
HISTORIAL = 288  # 24 h a una medicion cada 5 min

situacion = {"proxima": 0.0, "midiendo": None, "errores": {}}

# Clima de Bahia Blanca (Open-Meteo: modelo meteorologico, sin clave).
CLIMA_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=-38.72&longitude=-62.27"
    "&current=temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
    "&hourly=precipitation,precipitation_probability&past_hours=24&forecast_hours=7"
    "&timezone=America%2FArgentina%2FBuenos_Aires&wind_speed_unit=kmh")
clima = {"datos": None, "error": None, "t": 0.0}


def resumir_clima(j):
    c, h = j["current"], j["hourly"]
    ahora = c["time"][:13]  # hora en curso, "AAAA-MM-DDTHH"
    horas = [{"t": t, "mm": p or 0.0, "prob": pr} for t, p, pr in
             zip(h["time"], h["precipitation"], h.get("precipitation_probability") or [None] * len(h["time"]))]
    pasadas = [x for x in horas if x["t"][:13] < ahora]
    futuras = [x for x in horas if x["t"][:13] > ahora]
    return {
        "hora": c["time"], "temp": c["temperature_2m"],
        "viento": c["wind_speed_10m"], "rafagas": c["wind_gusts_10m"], "direccion": c["wind_direction_10m"],
        "lluvia_ahora": c["precipitation"],
        "lluvia_1h": sum(x["mm"] for x in pasadas[-1:]),
        "lluvia_6h": sum(x["mm"] for x in pasadas[-6:]),
        "lluvia_24h": sum(x["mm"] for x in pasadas[-24:]),
        "prevista_6h": sum(x["mm"] for x in futuras[:6]),
        "prob_max_6h": max((x["prob"] or 0 for x in futuras[:6]), default=0),
        "horas": horas,
    }


def bucle_clima():
    import urllib.request
    while True:
        try:
            with urllib.request.urlopen(CLIMA_URL, timeout=30) as r:
                clima["datos"] = resumir_clima(json.load(r))
            clima["error"], clima["t"] = None, time.time()
        except Exception as e:
            clima["error"] = f"{datetime.now():%d/%m %H:%M} {e}"
            print("clima error:", e, flush=True)
        time.sleep(300)


def bucle():
    previos = {cam: 0.0 for cam in CAMARAS}
    while True:
        for cam in CAMARAS:
            situacion["midiendo"] = cam
            try:
                previos[cam] = monitor.ciclo(cam, previos[cam])
                situacion["errores"].pop(cam, None)
            except Exception as e:
                situacion["errores"][cam] = f"{datetime.now():%d/%m %H:%M} {e}"
                print(f"[{cam}] error:", e, flush=True)
            monitor.limpiar_viejas(cam)
        situacion["midiendo"] = None
        situacion["proxima"] = time.time() + INTERVALO * 60
        time.sleep(INTERVALO * 60)


def resumen(cam):
    rutas = monitor.Rutas(cam)
    filas = []
    if rutas.registro.exists():
        with rutas.registro.open(encoding="utf-8") as f:
            filas = list(csv.DictReader(f))[-HISTORIAL:]
    actual = None
    if filas:
        u = filas[-1]
        actual = {
            "fecha_hora": u["fecha_hora"], "nivel": float(u["nivel_pct"]), "estado": u["estado"],
            "nota": u.get("nota", ""),
            "cortes": [float(u[k]) for k in u if k.startswith("corte_")],
        }
    return rutas, filas, actual


def lista_camaras():
    out = []
    for cam in CAMARAS:
        _, _, actual = resumen(cam)
        out.append({"id": cam, "nombre": monitor.CAMARAS[cam]["nombre"],
                    "nivel": actual and actual["nivel"], "estado": actual and actual["estado"]})
    return out


def estado_camara(cam):
    _, filas, actual = resumen(cam)
    return {
        "id": cam, "nombre": monitor.CAMARAS[cam]["nombre"], "video": monitor.CAMARAS[cam]["video"],
        "actual": actual,
        "historial": [{"t": r["fecha_hora"], "nivel": float(r["nivel_pct"]), "estado": r["estado"]} for r in filas],
        "camaras": lista_camaras(),
        "intervalo_min": INTERVALO, "proxima": situacion["proxima"], "ahora": time.time(),
        "midiendo": situacion["midiendo"] == cam, "error": situacion["errores"].get(cam),
        "clima": clima["datos"], "clima_error": clima["error"],
    }


class Manejador(BaseHTTPRequestHandler):
    def _enviar(self, cuerpo, tipo, codigo=200):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        partes = urlsplit(self.path)
        ruta, q = partes.path, parse_qs(partes.query)
        if PREFIJO and ruta == PREFIJO:
            self.send_response(301)
            self.send_header("Location", PREFIJO + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if PREFIJO and ruta.startswith(PREFIJO + "/"):
            ruta = ruta[len(PREFIJO):]
        cam = q.get("cam", [CAMARAS[0]])[0]
        if cam not in CAMARAS:
            return self._enviar(b"camara desconocida", "text/plain", 404)

        if ruta == "/":
            self._enviar((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif ruta == "/api/estado":
            self._enviar(json.dumps(estado_camara(cam)).encode(), "application/json")
        elif ruta == "/ultimo.jpg" and monitor.Rutas(cam).ultimo.exists():
            self._enviar(monitor.Rutas(cam).ultimo.read_bytes(), "image/jpeg")
        else:
            self._enviar(b"no encontrado", "text/plain", 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    monitor.migrar_datos_viejos()
    for cam in CAMARAS:
        monitor.Rutas(cam).crear()
    threading.Thread(target=bucle, daemon=True).start()
    threading.Thread(target=bucle_clima, daemon=True).start()
    print(f"Web en el puerto {PUERTO}, camaras {', '.join(CAMARAS)}, cada {INTERVALO:g} min", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
