"""Marea del estuario de Bahia Blanca, desde el Servicio de Hidrografia Naval (SHN).

- Prediccion de pleamares y bajamares (tablas de marea) para Puerto Ingeniero White
  y Puerto Belgrano.
- Alturas medidas cada hora por el mareografo de Puerto Belgrano (CSV publico).

Comparando lo medido en Puerto Belgrano con su prediccion se ve si el estuario esta
mas alto de lo normal (por ejemplo, por viento del sur o sudeste).
Horarios en hora oficial argentina; alturas en metros sobre el plano de reduccion de
cada puerto (no son comparables entre puertos).
"""
import html
import math
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

URL_TABLA = "https://www.hidro.gov.ar/oceanografia/Tmareas/RE_TablasDeMarea.asp"
URL_FORM = "https://www.hidro.gov.ar/oceanografia/Tmareas/Form_Tmareas.asp"
URL_MEDIDAS = "https://www.hidro.gov.ar/oceanografia/AlturasHorarias.asp?export=csv"
AGENTE = "Mozilla/5.0 (monitor de canales)"

PUERTOS = {"IWHI": "Puerto Ingeniero White", "BELG": "Puerto Belgrano"}


def _pedir(url, datos=None):
    req = urllib.request.Request(url, data=datos, headers={"User-Agent": AGENTE, "Referer": URL_FORM})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("latin-1")


def extremos_del_mes(puerto, anio, mes):
    """Lista de (datetime, altura) de pleamares y bajamares predichas para ese mes."""
    texto = _pedir(URL_TABLA, urllib.parse.urlencode(
        {"Localidad": puerto, "FAnio": anio, "FMes": mes}).encode())
    i = texto.find("Predicciones de las Pleamares")
    if i < 0:
        raise RuntimeError(f"el SHN no devolvio la tabla de {puerto} {mes}/{anio}")
    plano = re.sub(r"<[^>]+>", "|", re.sub(r"<script.*?</script>", "", texto[i:], flags=re.S))
    fichas = [html.unescape(t).strip() for t in plano.split("|")]
    fichas = [t for t in fichas if t]
    out, dia, k = [], None, 0
    while k < len(fichas):
        t = fichas[k]
        if re.fullmatch(r"\d{2}", t) and k + 1 < len(fichas) and re.fullmatch(r"\d{2}:\d{2}", fichas[k + 1]):
            dia = int(t)
        elif re.fullmatch(r"\d{2}:\d{2}", t) and dia and k + 1 < len(fichas) \
                and re.fullmatch(r"-?\d+,\d+", fichas[k + 1]):
            hh, mm = map(int, t.split(":"))
            out.append((datetime(anio, mes, dia, hh, mm), float(fichas[k + 1].replace(",", "."))))
            k += 1
        k += 1
    return out


def clasificar(extremos):
    """Marca cada extremo como pleamar o bajamar comparando con sus vecinos."""
    res = []
    for i, (t, h) in enumerate(extremos):
        vecinos = [extremos[j][1] for j in (i - 1, i + 1) if 0 <= j < len(extremos)]
        res.append({"t": t, "h": h, "tipo": "pleamar" if all(h > v for v in vecinos) else "bajamar"})
    return res


def altura_prevista(extremos, cuando):
    """Altura interpolada entre dos extremos (curva coseno, la usual para mareas)."""
    for a, b in zip(extremos, extremos[1:]):
        if a["t"] <= cuando <= b["t"]:
            f = (cuando - a["t"]).total_seconds() / (b["t"] - a["t"]).total_seconds()
            return a["h"] + (b["h"] - a["h"]) * (1 - math.cos(math.pi * f)) / 2
    return None


def medidas_belgrano():
    """[(datetime, altura)] de las ultimas horas en el mareografo de Puerto Belgrano."""
    filas = [l.split(";") for l in _pedir(URL_MEDIDAS).strip().splitlines()]
    col = next(i for i, c in enumerate(filas[0]) if "Belgrano" in c)
    out = []
    for f in filas[1:]:
        try:
            out.append((datetime.strptime(f[0].strip(), "%d/%m/%Y %H:%M"), float(f[col].replace(",", "."))))
        except (ValueError, IndexError):
            pass
    return sorted(out)


class Marea:
    """Guarda las tablas del mes (se piden pocas veces) y arma el resumen."""

    def __init__(self):
        self.tablas = {}      # (puerto, anio, mes) -> extremos

    def _extremos(self, puerto, ahora):
        meses = {(ahora.year, ahora.month)}
        for d in (-1, 2):     # incluye el borde con el mes anterior y el siguiente
            otro = ahora + timedelta(days=d)
            meses.add((otro.year, otro.month))
        todos = []
        for anio, mes in sorted(meses):
            clave = (puerto, anio, mes)
            if clave not in self.tablas:
                self.tablas[clave] = extremos_del_mes(puerto, anio, mes)
            todos += self.tablas[clave]
        return clasificar(sorted(todos))

    def resumen(self, ahora=None):
        ahora = ahora or datetime.now()
        white = self._extremos("IWHI", ahora)
        belg = self._extremos("BELG", ahora)

        h_ahora = altura_prevista(white, ahora)
        h_luego = altura_prevista(white, ahora + timedelta(minutes=20))
        proximos = [e for e in white if e["t"] > ahora][:4]
        prox_plea = next((e for e in proximos if e["tipo"] == "pleamar"), None)
        curva = []
        for m in range(-6 * 60, 18 * 60 + 1, 20):
            t = ahora + timedelta(minutes=m)
            h = altura_prevista(white, t)
            if h is not None:
                curva.append({"t": t.strftime("%Y-%m-%d %H:%M"), "h": round(h, 2)})

        medida = None
        try:
            obs = medidas_belgrano()
            if obs:
                t, h = obs[-1]
                prev = altura_prevista(belg, t)
                # Diferencia promedio de las ultimas 3 horas: suaviza el ruido y los
                # pequenos corrimientos de horario entre la marea real y la predicha.
                difs = [b - p for a, b in obs[-3:] if (p := altura_prevista(belg, a)) is not None]
                medida = {"t": t.strftime("%Y-%m-%d %H:%M"), "h": h,
                          "prevista": None if prev is None else round(prev, 2),
                          "diferencia": round(sum(difs) / len(difs), 2) if difs else None,
                          "serie": [{"t": a.strftime("%Y-%m-%d %H:%M"), "h": b} for a, b in obs[-12:]]}
        except Exception as e:  # el CSV del SHN a veces no responde: la prediccion sirve igual
            medida = {"error": str(e)}

        fmt = lambda e: {"t": e["t"].strftime("%Y-%m-%d %H:%M"), "h": e["h"], "tipo": e["tipo"]}
        return {
            "puerto": PUERTOS["IWHI"],
            "altura": None if h_ahora is None else round(h_ahora, 2),
            "tendencia": None if h_ahora is None or h_luego is None else ("sube" if h_luego > h_ahora else "baja"),
            "proximos": [fmt(e) for e in proximos],
            "proxima_pleamar": prox_plea and fmt(prox_plea),
            "curva": curva,
            "belgrano": medida,
        }
