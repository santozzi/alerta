"""Alertas oficiales del Servicio Meteorologico Nacional (SMN) en formato CAP.

El SMN publica en https://ssl.smn.gob.ar/CAP/AR.php la lista de alertas vigentes
(tormentas, viento, etc.) y los avisos a muy corto plazo, cada una como un XML CAP
con el poligono del area afectada. Aca se queda con las que cubren Bahia Blanca o
las sierras de la cuenca del Naposta y todavia no vencieron.

Color segun la severidad CAP (asi la usa el SMN): Moderate = amarilla,
Severe = naranja, Extreme = roja.
"""
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

LISTA = "https://ssl.smn.gob.ar/CAP/AR.php"
AGENTE = "Mozilla/5.0 (monitor de canales)"
NS = {"c": "urn:oasis:names:tc:emergency:cap:1.2"}

LUGARES = {
    "Bahía Blanca": (-38.72, -62.27),
    "Sierras de Ventania (cuenca alta)": (-38.07, -62.03),
}
COLOR = {"Minor": "amarilla", "Moderate": "amarilla", "Severe": "naranja", "Extreme": "roja"}
ORDEN = {"amarilla": 1, "naranja": 2, "roja": 3}

_cache = {}   # url -> alerta ya leida (los XML no cambian: cada version tiene otro nombre)
PAUSA = 2.5   # segundos entre pedidos: el SMN corta con 429 si se le piden muchos seguidos
ARCHIVO_CACHE = None   # si se define (Path), la cache se guarda en disco entre reinicios


def _cargar_cache():
    import json
    if ARCHIVO_CACHE and ARCHIVO_CACHE.exists() and not _cache:
        try:
            _cache.update(json.loads(ARCHIVO_CACHE.read_text(encoding="utf-8")))
        except ValueError:
            pass


def _guardar_cache():
    import json
    if ARCHIVO_CACHE:
        ARCHIVO_CACHE.write_text(json.dumps(_cache), encoding="utf-8")


def _pedir(url):
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _dentro(punto, poligono):
    """Punto (lat, lon) dentro de un poligono [(lat, lon), ...] (ray casting)."""
    y, x = punto
    adentro = False
    for (y1, x1), (y2, x2) in zip(poligono, poligono[1:] + poligono[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            adentro = not adentro
    return adentro


def _leer(url):
    raiz = ET.fromstring(_pedir(url))
    info = raiz.find("c:info", NS)
    if info is None:
        return None
    t = lambda tag: (info.findtext(f"c:{tag}", default="", namespaces=NS) or "").strip()
    poligonos = []
    for p in info.iterfind("c:area/c:polygon", NS):
        pts = []
        for par in (p.text or "").split():
            try:
                la, lo = par.split(",")
                pts.append((float(la), float(lo)))
            except ValueError:
                pass
        if len(pts) >= 3:
            poligonos.append(pts)
    lugares = [n for n, pt in LUGARES.items() if any(_dentro(pt, pol) for pol in poligonos)]
    return {
        "evento": t("event") or t("headline"), "severidad": t("severity"),
        "color": COLOR.get(t("severity"), "amarilla"),
        "desde": t("onset") or raiz.findtext("c:sent", default="", namespaces=NS),
        "hasta": t("expires"), "descripcion": t("description"), "instrucciones": t("instruction"),
        "corto_plazo": "avisocortoplazo" in url, "lugares": lugares, "url": url,
        # solo los poligonos que tocan la zona, para dibujarlos en el mapa
        "poligonos": [[[round(a, 3), round(b, 3)] for a, b in pol] for pol in poligonos
                      if any(_dentro(pt, pol) for pt in LUGARES.values())],
    }


def _fecha(s):
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def vigentes():
    """Alertas vigentes que afectan a Bahia Blanca o a las sierras, la mas grave primero."""
    html = _pedir(LISTA).decode("utf-8", "replace")
    urls = list(dict.fromkeys(re.findall(r'href="(https://ssl\.smn\.gob\.ar/feeds/CAP/[^"]+\.xml)"', html)))
    # Primero los avisos a corto plazo y la region Llanura (donde esta Bahia Blanca); Patagonia al final.
    urls.sort(key=lambda u: 0 if "avisocortoplazo" in u else 1 if "Llanura" in u else 2)
    ahora = datetime.now().astimezone()
    _cargar_cache()
    juntas, limitado, nuevos = {}, False, 0
    for u in urls:
        if u not in _cache and not limitado:
            time.sleep(PAUSA)
            try:
                _cache[u] = _leer(u)
                nuevos += 1
            except urllib.error.HTTPError as e:
                if e.code == 429:      # nos pide esperar: lo que falta se lee en la proxima vuelta
                    limitado = True
                continue
            except Exception:
                continue
        if u not in _cache:
            continue
        a = _cache[u]
        if not a or not a["lugares"]:
            continue
        hasta = _fecha(a["hasta"])
        if hasta and hasta < ahora:
            continue
        # El SMN publica la misma alerta en varios poligonos: se juntan y se suman los lugares.
        clave = (a["evento"], a["color"], a["desde"][:16], a["hasta"][:16], a["corto_plazo"])
        if clave in juntas:
            juntas[clave]["lugares"] = sorted(set(juntas[clave]["lugares"]) | set(a["lugares"]))
            juntas[clave]["poligonos"] = juntas[clave]["poligonos"] + [p for p in a.get("poligonos", [])
                                                                       if p not in juntas[clave]["poligonos"]]
        else:
            juntas[clave] = dict(a)
    for u in list(_cache):    # olvida los XML que ya no estan en la lista
        if u not in urls:
            del _cache[u]
    if nuevos:
        _guardar_cache()
    out = list(juntas.values())
    for a in out:
        desde = _fecha(a["desde"])
        a["vigente"] = desde is None or desde <= ahora
    out.sort(key=lambda a: (not a["vigente"], -ORDEN[a["color"]], a["desde"]))
    pendientes = sum(1 for u in urls if u not in _cache)
    return {"alertas": out, "completo": pendientes == 0, "pendientes": pendientes}


if __name__ == "__main__":
    r = vigentes()
    print("completo" if r["completo"] else f"faltan {r['pendientes']} XML")
    for a in r["alertas"]:
        print(a["color"].upper(), a["evento"], a["desde"], "->", a["hasta"], a["lugares"], "corto plazo" if a["corto_plazo"] else "")
