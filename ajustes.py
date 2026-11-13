"""Configuracion editable desde el panel de administracion.

Se guarda en datos/config.json y pisa los valores por defecto del codigo:
    intervalo_min   minutos entre rondas de medicion
    semaforo        umbrales de lluvia y marea del semaforo general
    camaras         por camara: nombre, video de YouTube, activa, lineas de medicion
                    (cortes) y donde esta el agua normalmente en cada linea (agua)
"""
import copy
import json
import re

import monitor

ARCHIVO = monitor.DATOS / "config.json"
ORIGINALES = copy.deepcopy(monitor.CAMARAS)   # las camaras tal como vienen en el codigo

SEMAFORO = {
    "lluvia_6h_mm": 10.0,    # lluvia prevista o caida en 6 h en la ciudad
    "lluvia_pico_mm": 5.0,   # lluvia prevista en una sola hora
    "sierras_24h_mm": 20.0,  # lluvia caida o prevista en 24 h en la cuenca alta
    "ciudad_24h_mm": 20.0,   # lluvia caida en 24 h en la ciudad (suelo mojado)
    "marea_m": 0.5,          # marea medida por encima de la prevista
}
MAX_NIVELES = 12
LIMITES_SEMAFORO = {"lluvia_6h_mm": (1, 200), "lluvia_pico_mm": (0.5, 100), "sierras_24h_mm": (1, 300),
                    "ciudad_24h_mm": (1, 300), "marea_m": (0.1, 3)}


def defecto(intervalo=5.0):
    return {"intervalo_min": float(intervalo), "semaforo": dict(SEMAFORO),
            "camaras": {cam: {**_original(cam)} for cam in ORIGINALES}}


def _original(cam):
    c = ORIGINALES[cam]
    return {"nombre": c["nombre"], "video": c["video"], "activa": True,
            "cortes": [[[int(a[0]), int(a[1])], [int(b[0]), int(b[1])]] for a, b in c["cortes"]],
            "agua": [[float(w0), float(w1)] for w0, w1 in c["agua"]],
            "niveles": []}   # lineas de nivel paralelas al canal; si hay, se mide con ellas


def validar(entrada, intervalo_defecto=5.0):
    """Devuelve una configuracion limpia a partir de lo que mando el panel, o lanza ValueError."""
    if not isinstance(entrada, dict):
        raise ValueError("configuracion invalida")
    cfg = defecto(intervalo_defecto)

    def numero(v, minimo, maximo, nombre):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not minimo <= v <= maximo:
            raise ValueError(f"{nombre}: tiene que ser un numero entre {minimo} y {maximo}")
        return float(v)

    if "intervalo_min" in entrada:
        cfg["intervalo_min"] = numero(entrada["intervalo_min"], 2, 60, "intervalo")
    for k, (lo, hi) in LIMITES_SEMAFORO.items():
        if k in (entrada.get("semaforo") or {}):
            cfg["semaforo"][k] = numero(entrada["semaforo"][k], lo, hi, k)

    for cam, e in (entrada.get("camaras") or {}).items():
        if cam not in ORIGINALES or not isinstance(e, dict):
            raise ValueError(f"camara desconocida: {cam}")
        c = cfg["camaras"][cam]
        if "nombre" in e:
            nombre = str(e["nombre"]).strip()
            if not 2 <= len(nombre) <= 60 or "<" in nombre or ">" in nombre:
                raise ValueError(f"{cam}: el nombre tiene que tener entre 2 y 60 caracteres")
            c["nombre"] = nombre
        if "video" in e:
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(e["video"])):
                raise ValueError(f"{cam}: el video es el codigo de 11 caracteres de YouTube")
            c["video"] = str(e["video"])
        if "activa" in e:
            c["activa"] = bool(e["activa"])
        n = len(c["cortes"])
        if "cortes" in e:
            cortes = e["cortes"]
            if not isinstance(cortes, list) or len(cortes) != n:
                raise ValueError(f"{cam}: tienen que ser {n} lineas")
            limpio = []
            for linea in cortes:
                try:
                    (x0, y0), (x1, y1) = linea
                    pts = [[int(round(x0)), int(round(y0))], [int(round(x1)), int(round(y1))]]
                except (TypeError, ValueError):
                    raise ValueError(f"{cam}: linea mal formada") from None
                if not all(0 <= x < monitor.ANCHO and 0 <= y < monitor.ALTO for x, y in pts):
                    raise ValueError(f"{cam}: una linea se sale de la imagen")
                if abs(pts[0][0] - pts[1][0]) + abs(pts[0][1] - pts[1][1]) < 40:
                    raise ValueError(f"{cam}: una linea es demasiado corta")
                limpio.append(pts)
            c["cortes"] = limpio
        if "agua" in e:
            agua = e["agua"]
            if not isinstance(agua, list) or len(agua) != n:
                raise ValueError(f"{cam}: falta el agua de base de alguna linea")
            limpio = []
            for par in agua:
                try:
                    w0, w1 = float(par[0]), float(par[1])
                except (TypeError, ValueError, IndexError):
                    raise ValueError(f"{cam}: agua de base mal formada") from None
                if not 0 <= w0 < w1 <= 1:
                    raise ValueError(f"{cam}: el agua de base tiene que ir de izquierda a derecha, entre 0 y 1")
                limpio.append([round(w0, 3), round(w1, 3)])
            c["agua"] = limpio
        if "niveles" in e:
            niveles = e["niveles"]
            if not isinstance(niveles, list) or len(niveles) > MAX_NIVELES:
                raise ValueError(f"{cam}: puede haber hasta {MAX_NIVELES} lineas de nivel")
            limpio = []
            for linea in niveles:
                try:
                    pct = float(linea["pct"])
                    (x0, y0), (x1, y1) = linea["puntos"]
                    pts = [[int(round(x0)), int(round(y0))], [int(round(x1)), int(round(y1))]]
                except (TypeError, ValueError, KeyError):
                    raise ValueError(f"{cam}: linea de nivel mal formada") from None
                if not 1 <= pct <= 100:
                    raise ValueError(f"{cam}: el porcentaje de cada linea de nivel va de 1 a 100")
                if not all(0 <= x < monitor.ANCHO and 0 <= y < monitor.ALTO for x, y in pts):
                    raise ValueError(f"{cam}: una linea de nivel se sale de la imagen")
                if abs(pts[0][0] - pts[1][0]) + abs(pts[0][1] - pts[1][1]) < 40:
                    raise ValueError(f"{cam}: una linea de nivel es demasiado corta")
                limpio.append({"pct": round(pct, 1), "puntos": pts})
            c["niveles"] = sorted(limpio, key=lambda n: n["pct"])
    if not any(c["activa"] for c in cfg["camaras"].values()):
        raise ValueError("tiene que quedar al menos una camara activa")
    return cfg


def cargar(intervalo_defecto=5.0):
    try:
        return validar(json.loads(ARCHIVO.read_text(encoding="utf-8")), intervalo_defecto)
    except (OSError, ValueError):
        return defecto(intervalo_defecto)


def guardar(cfg):
    ARCHIVO.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARCHIVO.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARCHIVO)


def aplicar(cfg):
    """Lleva la configuracion al medidor (monitor.CAMARAS se actualiza en el lugar)."""
    for cam, c in cfg["camaras"].items():
        m = monitor.CAMARAS[cam]
        cambio_video = m["video"] != c["video"]
        m.update(nombre=c["nombre"], video=c["video"], activa=c["activa"],
                 cortes=[(tuple(a), tuple(b)) for a, b in c["cortes"]],
                 agua=[tuple(w) for w in c["agua"]],
                 niveles=[{"pct": n["pct"], "puntos": [tuple(n["puntos"][0]), tuple(n["puntos"][1])]}
                          for n in c["niveles"]])
        if cambio_video:
            monitor._urls.pop(cam, None)   # la direccion del stream guardada ya no sirve
