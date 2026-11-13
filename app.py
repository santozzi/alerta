"""Servidor web + bucle de medicion de los canales (pensado para correr en Docker)."""
import csv
import json
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import admin
import ajustes
import monitor

INTERVALO_DEFECTO = float(os.environ.get("INTERVALO_MIN", "5"))
PUERTO = int(os.environ.get("PUERTO", "8000"))
# Ruta publica cuando va detras de un proxy (ej. "/alerta"); vacio = raiz.
PREFIJO = os.environ.get("PREFIJO", "").rstrip("/")
# Camaras a vigilar (separadas por coma); vacio = todas.
PERMITIDAS = [c for c in os.environ.get("CAMARAS", "").split(",") if c in monitor.CAMARAS] or list(monitor.CAMARAS)
# Configuracion editable desde el panel de administracion (datos/config.json).
CFG = ajustes.cargar(INTERVALO_DEFECTO)
ajustes.aplicar(CFG)
CAMARAS = [c for c in PERMITIDAS if CFG["camaras"][c]["activa"]] or PERMITIDAS[:1]


def recargar_config(cfg):
    """Pone en uso una configuracion nueva sin reiniciar el servidor."""
    CFG.clear()
    CFG.update(cfg)
    ajustes.aplicar(CFG)
    CAMARAS[:] = [c for c in PERMITIDAS if CFG["camaras"][c]["activa"]] or PERMITIDAS[:1]
    for cam in CAMARAS:
        monitor.Rutas(cam).crear()


WEB = Path(__file__).resolve().parent / "web"
HISTORIAL = 288  # 24 h a una medicion cada 5 min

situacion = {"proxima": 0.0, "midiendo": None, "errores": {}}

# Clima de Bahia Blanca (Open-Meteo: modelo meteorologico, sin clave).
CLIMA_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=-38.72&longitude=-62.27"
    "&current=temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
    "&hourly=precipitation,precipitation_probability&past_hours=24&forecast_hours=7"
    "&minutely_15=wind_speed_10m,wind_direction_10m,wind_gusts_10m&past_minutely_15=2&forecast_minutely_15=1"
    "&timezone=America%2FArgentina%2FBuenos_Aires&wind_speed_unit=kmh")
clima = {"datos": None, "error": None, "t": 0.0}
marea_estado = {"datos": None, "error": None}
SOBREELEVACION = 0.5   # m por encima de lo previsto en Puerto Belgrano para avisar


noticias_estado = {"notas": [], "error": None, "t": None}
smn_estado = {"alertas": None, "completo": False, "error": None, "t": None}


def bucle_smn():
    import smn
    smn.ARCHIVO_CACHE = monitor.DATOS / "smn_cache.json"
    while True:
        try:
            r = smn.vigentes()
            smn_estado.update(alertas=r["alertas"], completo=r["completo"], error=None,
                              t=datetime.now().strftime("%H:%M"))
        except Exception as e:
            smn_estado["error"] = f"{datetime.now():%d/%m %H:%M} {e}"
            print("smn error:", e, flush=True)
        # Si todavia faltan alertas por leer (el SMN limita), vuelve antes.
        time.sleep(900 if smn_estado["completo"] else 120)


def bucle_noticias():
    import noticias as n
    while True:
        try:
            noticias_estado["notas"] = n.buscar()
            noticias_estado["error"], noticias_estado["t"] = None, datetime.now().strftime("%H:%M")
        except Exception as e:
            noticias_estado["error"] = f"{datetime.now():%d/%m %H:%M} {e}"
            print("noticias error:", e, flush=True)
        time.sleep(600)


def bucle_marea():
    import marea as m
    fuente = m.Marea()
    while True:
        try:
            marea_estado["datos"], marea_estado["error"] = fuente.resumen(), None
        except Exception as e:
            marea_estado["error"] = f"{datetime.now():%d/%m %H:%M} {e}"
            print("marea error:", e, flush=True)
        time.sleep(900)   # el mareografo publica una vez por hora

# Lluvia en la cuenca del Napostá: la lluvia en las sierras llega a la ciudad
# horas despues. Coordenadas aproximadas, elegidas a mano.
CUENCA = [
    ("Cuenca alta (sierras de Ventania)", -38.07, -62.03),
    ("Cuenca media (al norte de la ciudad)", -38.25, -62.20),
    ("Bahía Blanca", -38.72, -62.27),
]
CUENCA_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(str(p[1]) for p in CUENCA)
    + "&longitude=" + ",".join(str(p[2]) for p in CUENCA)
    + "&hourly=precipitation&past_hours=72&forecast_hours=24"
    "&timezone=America%2FArgentina%2FBuenos_Aires")


def resumir_cuenca(lista, hora_actual):
    out = []
    for (nombre, lat, lon), j in zip(CUENCA, lista):
        h = j["hourly"]
        pas = [p or 0.0 for t, p in zip(h["time"], h["precipitation"]) if t[:13] < hora_actual]
        fut = [p or 0.0 for t, p in zip(h["time"], h["precipitation"]) if t[:13] > hora_actual]
        out.append({
            "nombre": nombre, "elevacion": j.get("elevation"),
            "lluvia_24h": sum(pas[-24:]), "lluvia_72h": sum(pas[-72:]),
            "prevista_6h": sum(fut[:6]), "prevista_24h": sum(fut[:24]),
            "pico_previsto": max(fut[:24], default=0.0),
        })
    return out


def resumir_clima(j):
    c, h = j["current"], j["hourly"]
    ahora = c["time"][:13]  # hora en curso, "AAAA-MM-DDTHH"
    horas = [{"t": t, "mm": p or 0.0, "prob": pr} for t, p, pr in
             zip(h["time"], h["precipitation"], h.get("precipitation_probability") or [None] * len(h["time"]))]
    pasadas = [x for x in horas if x["t"][:13] < ahora]
    futuras = [x for x in horas if x["t"][:13] > ahora]
    # Viento del intervalo anterior (15 min antes del actual), para comparar en la rosa.
    anterior = None
    m = j.get("minutely_15") or {}
    previos = [(t, v, d, g) for t, v, d, g in zip(m.get("time", []), m.get("wind_speed_10m", []),
                                                    m.get("wind_direction_10m", []), m.get("wind_gusts_10m", []))
               if t < c["time"] and v is not None and d is not None]
    if previos:
        t, v, d, g = previos[-1]
        anterior = {"hora": t, "viento": v, "direccion": d, "rafagas": g}
    return {
        "viento_anterior": anterior,
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
                datos = resumir_clima(json.load(r))
            with urllib.request.urlopen(CUENCA_URL, timeout=30) as r:
                datos["cuenca"] = resumir_cuenca(json.load(r), datos["hora"][:13])
            clima["datos"] = datos
            clima["error"], clima["t"] = None, time.time()
        except Exception as e:
            clima["error"] = f"{datetime.now():%d/%m %H:%M} {e}"
            print("clima error:", e, flush=True)
        time.sleep(300)


def bucle():
    previos = {}
    while True:
        for cam in list(CAMARAS):
            situacion["midiendo"] = cam
            try:
                previos[cam] = monitor.ciclo(cam, previos.get(cam, 0.0))
                situacion["errores"].pop(cam, None)
            except Exception as e:
                situacion["errores"][cam] = f"{datetime.now():%d/%m %H:%M} {e}"
                print(f"[{cam}] error:", e, flush=True)
            monitor.limpiar_viejas(cam)
        situacion["midiendo"] = None
        espera = CFG["intervalo_min"] * 60
        situacion["proxima"] = time.time() + espera
        time.sleep(espera)


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


def _pendiente(puntos):
    """Pendiente por minimos cuadrados en %/hora; puntos = [(minutos, nivel)]."""
    if len(puntos) < 3 or puntos[-1][0] - puntos[0][0] < 9:
        return None
    t = [p[0] for p in puntos]; n = [p[1] for p in puntos]
    mt, mn = sum(t) / len(t), sum(n) / len(n)
    den = sum((x - mt) ** 2 for x in t)
    return 60.0 * sum((x - mt) * (y - mn) for x, y in zip(t, n)) / den if den else None


def tendencia(filas):
    """Velocidad de subida del nivel en %/hora en los ultimos 30 y 60 minutos."""
    if not filas:
        return None
    pts = []
    for r in filas:
        if r.get("estado") == "NO_CONFIABLE":
            continue   # capturas obstruidas o con la camara movida: no cuentan para el ritmo
        try:
            pts.append((datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M"), float(r["nivel_pct"])))
        except (KeyError, ValueError):
            pass
    if not pts:
        return None
    fin = pts[-1][0]
    def ventana(minutos):
        return [((t - fin).total_seconds() / 60, n) for t, n in pts if (fin - t).total_seconds() <= minutos * 60]
    v30, v60 = _pendiente(ventana(30)), _pendiente(ventana(60))
    v = v30 if v30 is not None else v60
    if v is None:
        etiqueta = "sin datos"
    elif v >= 20:
        etiqueta = "sube rápido"
    elif v >= 5:
        etiqueta = "sube"
    elif v <= -5:
        etiqueta = "baja"
    else:
        etiqueta = "estable"
    nivel = pts[-1][1]
    # Minutos hasta el proximo umbral a este ritmo (solo si sube).
    eta = None
    if v is not None and v >= 5:
        prox = next((u for u in (15, 40, 70, 100) if u > nivel), None)
        if prox is not None:
            eta = {"umbral": prox, "minutos": round((prox - nivel) / v * 60)}
    return {"v30": v30, "v60": v60, "v": v, "etiqueta": etiqueta, "eta": eta}


def lista_camaras():
    out = []
    for cam in CAMARAS:
        _, filas, actual = resumen(cam)
        tend = tendencia(filas)
        out.append({"id": cam, "nombre": monitor.CAMARAS[cam]["nombre"],
                    "nivel": actual and actual["nivel"], "estado": actual and actual["estado"],
                    "tendencia": tend and tend["etiqueta"]})
    return out


def estado_camara(cam):
    _, filas, actual = resumen(cam)
    return {
        "id": cam, "nombre": monitor.CAMARAS[cam]["nombre"], "video": monitor.CAMARAS[cam]["video"],
        "actual": actual,
        "tendencia": tendencia(filas),
        "historial": [{"t": r["fecha_hora"], "nivel": float(r["nivel_pct"]), "estado": r["estado"]} for r in filas
                      if r["estado"] != "NO_CONFIABLE"],
        "camaras": lista_camaras(),
        "intervalo_min": CFG["intervalo_min"], "proxima": situacion["proxima"], "ahora": time.time(),
        "midiendo": situacion["midiendo"] == cam,
        "midiendo_otra": (monitor.CAMARAS[situacion["midiendo"]]["nombre"]
                          if situacion["midiendo"] and situacion["midiendo"] != cam else None),
        "error": situacion["errores"].get(cam),
        "clima": clima["datos"], "clima_error": clima["error"],
        "riesgo_nivel": resumen_general()["riesgo"]["nivel"],
    }


def resumen_general():
    """Todo lo necesario para el panel general: cada camara, el clima y el riesgo."""
    camaras = []
    for cam in CAMARAS:
        rutas, filas, actual = resumen(cam)
        try:
            tramos = json.loads(rutas.ultimo_json.read_text(encoding="utf-8"))["tramos"]
        except (OSError, ValueError, KeyError):
            tramos = None
        camaras.append({
            "id": cam, "nombre": monitor.CAMARAS[cam]["nombre"], "video": monitor.CAMARAS[cam]["video"],
            # lineas de medicion en coordenadas de la imagen 1920x1080, y que partes cambiaron
            "lineas": [[[int(v) for v in a], [int(v) for v in b]] for a, b in monitor.CAMARAS[cam]["cortes"]],
            "tramos": tramos,
            "actual": actual, "tendencia": tendencia(filas),
            # ultimas 6 h para el mini grafico
            "historial": [{"t": r["fecha_hora"], "nivel": float(r["nivel_pct"])} for r in filas[-72:]
                          if r["estado"] != "NO_CONFIABLE"],
            "midiendo": situacion["midiendo"] == cam, "error": situacion["errores"].get(cam),
        })
    return {
        "camaras": camaras, "clima": clima["datos"], "clima_error": clima["error"],
        "marea": marea_estado["datos"], "marea_error": marea_estado["error"],
        "smn": smn_estado,
        "riesgo": riesgo(camaras, clima["datos"], marea_estado["datos"]),
        "intervalo_min": CFG["intervalo_min"], "proxima": situacion["proxima"], "ahora": time.time(),
        "midiendo": situacion["midiendo"] and monitor.CAMARAS[situacion["midiendo"]]["nombre"],
    }


ORDEN_ESTADO = {"normal": 0, "SUBIENDO": 1, "ALTO": 2, "CRITICO": 3}


def riesgo(camaras, k, mar=None):
    """Semaforo general: el peor canal, y 'atencion' por lluvia aunque el agua no haya subido.

    Devuelve nivel (0 normal, 1 atencion, 2 alerta, 3 critico) y los motivos en texto.
    """
    nivel, motivos = 0, []
    s = CFG["semaforo"]
    for c in camaras:
        a, t = c["actual"], c["tendencia"]
        if not a:
            continue
        e = ORDEN_ESTADO.get(a["estado"], 0)
        if e >= 3:
            nivel = max(nivel, 3); motivos.append(f"{c['nombre']}: nivel crítico ({a['nivel']:.0f} %)")
        elif e == 2:
            nivel = max(nivel, 2); motivos.append(f"{c['nombre']}: nivel alto ({a['nivel']:.0f} %)")
        elif e == 1:
            nivel = max(nivel, 1); motivos.append(f"{c['nombre']}: subiendo ({a['nivel']:.0f} %)")
        if t and t.get("etiqueta") == "sube rápido":
            nivel = max(nivel, 2); motivos.append(f"{c['nombre']}: sube rápido ({t['v']:+.0f} %/h)")
    # Alertas oficiales del SMN (CAP). Amarilla vigente = atencion; naranja o roja vigente = alerta.
    # Si todavia no se pudieron leer, se usan como respaldo los titulos de las noticias.
    # Mientras la lectura este incompleta (el SMN limita), no se puede afirmar que no hay alertas.
    alertas = smn_estado["alertas"] if smn_estado["completo"] else None
    if alertas is not None:
        for a in alertas:
            if not a["vigente"]:
                continue
            nivel = max(nivel, 1 if a["color"] == "amarilla" else 2)
            hasta = a["hasta"][11:16] if a["hasta"] else "?"
            motivos.append(f"SMN: alerta {a['color']} por {a['evento'].lower()} en {', '.join(a['lugares'])} (hasta las {hasta})")
        proximas = [a for a in alertas if not a["vigente"] and a["color"] != "amarilla"]
        if proximas:
            a = proximas[0]
            nivel = max(nivel, 1)
            motivos.append(f"SMN: alerta {a['color']} por {a['evento'].lower()} desde las {a['desde'][11:16]}"
                           f"{' del ' + a['desde'][8:10] + '/' + a['desde'][5:7] if a['desde'][:10] != datetime.now().strftime('%Y-%m-%d') else ''}")
    else:
        hace12 = (datetime.now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")
        for color, minimo in (("roja", 2), ("naranja", 1)):
            nota = next((n for n in noticias_estado["notas"]
                         if n["fecha"] >= hace12 and f"alerta {color}" in n["titulo"].lower()), None)
            if nota:
                nivel = max(nivel, minimo)
                motivos.append(f"Medios informan alerta {color} por tormentas ({nota['medio']}, {nota['fecha'][11:]})")
                break
    lluvia_fuerte = False
    if k:
        lluvia_fuerte = k["prevista_6h"] >= s["lluvia_6h_mm"] or k["lluvia_6h"] >= s["lluvia_6h_mm"] or max(
            (h["mm"] for h in k["horas"] if h["t"][:13] > k["hora"][:13]), default=0) >= s["lluvia_pico_mm"]
    if mar:
        b = mar.get("belgrano") or {}
        reciente = b.get("t") and datetime.now() - datetime.strptime(b["t"], "%Y-%m-%d %H:%M") < timedelta(hours=3)
        if reciente and b.get("diferencia") is not None and b["diferencia"] >= s["marea_m"]:
            nivel = max(nivel, 1)
            motivos.append(f"Marea {b['diferencia']:+.2f} m por encima de lo previsto (Puerto Belgrano)".replace(".", ","))
        p = mar.get("proxima_pleamar")
        if lluvia_fuerte and p:
            horas = (datetime.strptime(p["t"], "%Y-%m-%d %H:%M") - datetime.now()).total_seconds() / 3600
            if 0 <= horas <= 6:
                nivel = max(nivel, 1)
                motivos.append(f"Lluvia fuerte con pleamar a las {p['t'][11:]} ({p['h']:.2f} m)".replace(".", ",")
                               + ": los canales desaguan peor")
    if k:
        alta = next((p for p in k.get("cuenca", []) if p["nombre"].startswith("Cuenca alta")), None)
        if k["prevista_6h"] >= s["lluvia_6h_mm"] or max((h["mm"] for h in k["horas"] if h["t"][:13] > k["hora"][:13]),
                                                         default=0) >= s["lluvia_pico_mm"]:
            nivel = max(nivel, 1); motivos.append(f"Lluvia fuerte prevista en la ciudad ({k['prevista_6h']:.1f} mm en 6 h)")
        if alta and (alta["prevista_24h"] >= s["sierras_24h_mm"] or alta["lluvia_24h"] >= s["sierras_24h_mm"]):
            nivel = max(nivel, 1)
            motivos.append(f"Mucha lluvia en las sierras (24 h: {alta['lluvia_24h']:.1f} mm caídos, {alta['prevista_24h']:.1f} mm previstos)")
        if k["lluvia_24h"] >= s["ciudad_24h_mm"]:
            nivel = max(nivel, 1); motivos.append(f"Suelo mojado: {k['lluvia_24h']:.1f} mm en las últimas 24 h")
    return {"nivel": nivel, "motivos": motivos}


class Manejador(BaseHTTPRequestHandler):
    def _enviar(self, cuerpo, tipo, codigo=200, cache="no-store"):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", cache)
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
        if ruta.startswith("/admin"):
            return self._admin_get(ruta, q)
        cam = q.get("cam", [CAMARAS[0]])[0]
        if cam not in CAMARAS:
            return self._enviar(b"camara desconocida", "text/plain", 404)

        if ruta == "/":
            self._enviar((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif ruta in ("/panel", "/panel/"):
            # El panel general ahora es la solapa inicial de la pagina principal.
            self.send_response(301)
            self.send_header("Location", (PREFIJO or "") + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif ruta == "/mapa.json":
            self._enviar((WEB / "mapa.json").read_bytes(), "application/json", cache="public, max-age=3600")
        elif ruta in ("/logo.png", "/favicon.png", "/logo-completo.png"):
            self._enviar((WEB / ruta[1:]).read_bytes(), "image/png", cache="public, max-age=86400")
        elif ruta == "/api/noticias":
            self._enviar(json.dumps(noticias_estado).encode(), "application/json")
        elif ruta == "/api/resumen":
            self._enviar(json.dumps(resumen_general()).encode(), "application/json")
        elif ruta == "/api/estado":
            self._enviar(json.dumps(estado_camara(cam)).encode(), "application/json")
        elif ruta == "/ultimo.jpg" and monitor.Rutas(cam).ultimo.exists():
            cuerpo = monitor.Rutas(cam).ultimo.read_bytes()
            ancho = q.get("ancho", [""])[0]
            if ancho.isdigit() and 64 <= int(ancho) < 1920:
                import cv2
                import numpy as np
                img = cv2.imdecode(np.frombuffer(cuerpo, np.uint8), cv2.IMREAD_COLOR)
                alto = round(img.shape[0] * int(ancho) / img.shape[1])
                cuerpo = cv2.imencode(".jpg", cv2.resize(img, (int(ancho), alto), interpolation=cv2.INTER_AREA),
                                      [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
            # La URL lleva la hora de la medicion (t=...), asi que se puede guardar en cache.
            self._enviar(cuerpo, "image/jpeg", cache="public, max-age=86400" if "t" in q else "no-store")
        else:
            self._enviar(b"no encontrado", "text/plain", 404)

    # ------------------------------------------------------------ administracion
    def _json(self, datos, codigo=200, cabeceras=()):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        for k, v in cabeceras:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _ip(self):
        return (self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
                or self.client_address[0])

    def _token(self):
        for parte in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = parte.strip().partition("=")
            if k == admin.COOKIE:
                return v
        return None

    def _cookie(self, token, duracion):
        seguro = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return ("Set-Cookie", f"{admin.COOKIE}={token}; Path={PREFIJO or '/'}; Max-Age={duracion}; HttpOnly; SameSite=Strict{seguro}")

    def _admin_get(self, ruta, q):
        if ruta in ("/admin", "/admin/"):
            if ruta == "/admin":   # con la barra final, los enlaces relativos de la pagina funcionan
                self.send_response(301)
                self.send_header("Location", PREFIJO + "/admin/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            return self._enviar((WEB / "admin.html").read_bytes(), "text/html; charset=utf-8")
        if ruta == "/admin/api/sesion":
            return self._json({"activo": admin.clave_configurada(), "dentro": admin.valida(self._token())})
        if not admin.valida(self._token()):
            return self._json({"error": "Hay que ingresar con la clave."}, 401)
        if ruta == "/admin/api/config":
            return self._json({"config": CFG, "defecto": ajustes.defecto(INTERVALO_DEFECTO), "permitidas": PERMITIDAS,
                               "limites": ajustes.LIMITES_SEMAFORO, "ancho": monitor.ANCHO, "alto": monitor.ALTO})
        if ruta == "/admin/api/base":
            cam = q.get("cam", [""])[0]
            if cam not in monitor.CAMARAS:
                return self._json({"error": "camara desconocida"}, 404)
            # La imagen sobre la que se marcan las lineas: la base o, si no hay, la ultima captura limpia.
            for archivo in (monitor.BASES / f"{cam}.jpg", monitor.Rutas(cam).dir / "previa.jpg"):
                if archivo.exists():
                    return self._enviar(archivo.read_bytes(), "image/jpeg")
            return self._json({"error": "todavia no hay imagen de esta camara"}, 404)
        return self._json({"error": "no encontrado"}, 404)

    def do_POST(self):
        ruta = urlsplit(self.path).path
        if PREFIJO and ruta.startswith(PREFIJO + "/"):
            ruta = ruta[len(PREFIJO):]
        if not ruta.startswith("/admin/api/"):
            return self._json({"error": "no encontrado"}, 404)
        # Solo se aceptan pedidos hechos desde la propia pagina (freno a pedidos cruzados).
        origen = self.headers.get("Origin")
        if origen and urlsplit(origen).netloc != (self.headers.get("X-Forwarded-Host") or self.headers.get("Host")):
            return self._json({"error": "origen no permitido"}, 403)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json({"error": "formato no permitido"}, 415)
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            if not 0 < largo <= 100_000:
                raise ValueError
            datos = json.loads(self.rfile.read(largo))
            if not isinstance(datos, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "pedido invalido"}, 400)

        if ruta == "/admin/api/ingresar":
            if not admin.clave_configurada():
                return self._json({"error": "El panel esta desactivado: falta ADMIN_CLAVE en el .env."}, 403)
            ip = self._ip()
            espera = admin.bloqueado(ip)
            if espera:
                return self._json({"error": f"Demasiados intentos. Proba de nuevo en {espera // 60 + 1} min."}, 429)
            token = admin.ingresar(datos.get("clave"), ip)
            if not token:
                time.sleep(1)   # hace mas lento probar claves
                print(f"admin: clave incorrecta desde {ip}", flush=True)
                return self._json({"error": "Clave incorrecta."}, 401)
            print(f"admin: ingreso desde {ip}", flush=True)
            return self._json({"ok": True}, cabeceras=[self._cookie(token, admin.DURACION)])

        if not admin.valida(self._token()):
            return self._json({"error": "Hay que ingresar con la clave."}, 401)
        if ruta == "/admin/api/salir":
            admin.salir(self._token())
            return self._json({"ok": True}, cabeceras=[self._cookie("", 0)])
        if ruta == "/admin/api/config":
            try:
                cfg = ajustes.validar(datos.get("config"), INTERVALO_DEFECTO)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            ajustes.guardar(cfg)
            recargar_config(cfg)
            print(f"admin: configuracion guardada desde {self._ip()}", flush=True)
            return self._json({"ok": True, "config": CFG})
        if ruta == "/admin/api/recalibrar":
            cam = datos.get("cam")
            if cam not in monitor.CAMARAS:
                return self._json({"error": "camara desconocida"}, 404)
            rutas = monitor.Rutas(cam)
            borradas = 0
            for ref in rutas.refs.glob("ref_*.jpg"):
                ref.unlink()
                borradas += 1
            (rutas.dir / "calidad.json").unlink(missing_ok=True)
            print(f"admin: {cam} recalibrada ({borradas} referencias borradas)", flush=True)
            return self._json({"ok": True, "borradas": borradas})
        return self._json({"error": "no encontrado"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    monitor.migrar_datos_viejos()
    for cam in CAMARAS:
        monitor.Rutas(cam).crear()
    threading.Thread(target=bucle, daemon=True).start()
    threading.Thread(target=bucle_clima, daemon=True).start()
    threading.Thread(target=bucle_marea, daemon=True).start()
    threading.Thread(target=bucle_noticias, daemon=True).start()
    threading.Thread(target=bucle_smn, daemon=True).start()
    import telegram_bot
    bot = telegram_bot.desde_entorno(monitor.DATOS, resumen_general, lambda cam: monitor.Rutas(cam).ultimo,
                                     os.environ.get("URL_PUBLICA", ""))
    if bot:
        # Espera a que haya clima, marea y alertas cargadas antes del primer aviso.
        threading.Timer(90, bot.arrancar).start()
        print("Telegram: bot activo" + ("" if bot.chat_id else " (esperando que le escribas para registrar tu chat)"), flush=True)
    else:
        print("Telegram: desactivado (no hay TELEGRAM_BOT_TOKEN en el .env); la pagina funciona igual", flush=True)
    print("Administracion: " + ("activa en /admin" if admin.clave_configurada()
                                 else "desactivada (falta ADMIN_CLAVE de 8 caracteres o mas en el .env)"), flush=True)
    print(f"Web en el puerto {PUERTO}, camaras {', '.join(CAMARAS)}, cada {CFG['intervalo_min']:g} min", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
