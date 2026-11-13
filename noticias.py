"""Noticias sobre lluvia, inundaciones y crecidas en Bahia Blanca y la zona.

Busca en Google Noticias (RSS publico) y filtra por palabras clave: la nota tiene
que nombrar un lugar de la zona y algo relacionado con agua o mal tiempo, y no
ser de deportes (en Bahia hay un club que se llama Naposta).
"""
import html
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

AGENTE = "Mozilla/5.0 (monitor de canales)"
DIAS = 3

LUGARES = ["bahia blanca", "sierra de la ventana", "ingeniero white", "general cerri", "cerri",
           "tornquist", "villa ventana", "saldungaray", "punta alta", "monte hermoso",
           "villarino", "coronel rosales", "sudoeste bonaerense", "maldonado", "naposta"]
AGUA = ["lluvia", "llovio", "llueve", "lluvioso", "precipitacion", "milimetro", "inundac", "inundad",
        "anegamient", "anegad", "temporal", "tormenta", "crecida", "desborde", "desbord",
        "alerta amarilla", "alerta naranja", "alerta roja", "alerta meteorologic", "granizo",
        "evacuad", "barro", "canal maldonado", "arroyo naposta", "napostá", "sauce chico",
        "defensa civil", "calles cortadas", "agua acumulada", "sudestada", "marea"]
FUERA = ["basquet", "básquet", "basket", "lbb", "liga ", "futbol", "fútbol", "partido", "torneo",
         "cuartos de final", "semifinal", "club ", "rugby", "hockey", "voley", "tenis"]
# Palabras que marcan una nota como urgente en la lista.
URGENTE = ["inundac", "inundad", "evacuad", "alerta naranja", "alerta roja", "desborde", "desbord", "crecida"]

CONSULTAS = [
    '"Bahía Blanca" (lluvia OR inundación OR anegamiento OR temporal OR tormenta OR crecida OR "canal Maldonado" OR "arroyo Napostá")',
    '("Sierra de la Ventana" OR Tornquist OR "Villa Ventana" OR Saldungaray) (lluvia OR crecida OR temporal OR tormenta OR inundación)',
    '("Ingeniero White" OR Cerri OR "Punta Alta" OR "sudoeste bonaerense") (lluvia OR inundación OR temporal OR alerta)',
]


def _normal(t):
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _contiene(texto, palabras):
    return [p for p in palabras if _normal(p) in texto]


def _buscar(consulta):
    q = urllib.parse.quote(f"{consulta} when:{DIAS}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=es-419&gl=AR&ceid=AR:es-419"
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=30) as r:
        raiz = ET.fromstring(r.read())
    for it in raiz.iter("item"):
        yield it


def buscar():
    """Lista de notas filtradas, mas nuevas primero."""
    vistas, notas = set(), []
    limite = datetime.now(timezone.utc) - timedelta(days=DIAS)
    for consulta in CONSULTAS:
        for it in _buscar(consulta):
            titulo = html.unescape(it.findtext("title") or "").strip()
            medio = (it.findtext("source") or "").strip()
            if medio and titulo.endswith(" - " + medio):
                titulo = titulo[: -len(medio) - 3].strip()
            resumen = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
            texto = _normal(titulo + " " + resumen)
            clave = _normal(titulo)[:80]
            if clave in vistas:
                continue
            try:
                fecha = parsedate_to_datetime(it.findtext("pubDate"))
            except (TypeError, ValueError):
                continue
            if fecha < limite:
                continue
            # Deportes afuera; tiene que haber lugar de la zona y tema de agua o mal tiempo.
            if _contiene(_normal(titulo), FUERA):
                continue
            agua = _contiene(texto, AGUA)
            # "Naposta" o "Maldonado" solos no alcanzan como tema de agua (son tambien nombres).
            agua = [a for a in agua if a not in ("napostá",)] or (
                agua if "arroyo" in texto or "canal" in texto else [])
            if not _contiene(texto, LUGARES) or not agua:
                continue
            vistas.add(clave)
            notas.append({
                "titulo": titulo, "medio": medio, "link": it.findtext("link"),
                "fecha": fecha.astimezone().strftime("%Y-%m-%d %H:%M"),
                "urgente": bool(_contiene(_normal(titulo), URGENTE)),
            })
    notas.sort(key=lambda n: n["fecha"], reverse=True)
    return notas[:40]


if __name__ == "__main__":
    for n in buscar():
        print(n["fecha"], "!" if n["urgente"] else " ", n["medio"][:20].ljust(20), n["titulo"][:100])
