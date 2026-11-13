"""Monitor del nivel de agua de canales (camaras publicas en YouTube).

Cada N minutos captura cada camara, compara la textura del canal contra una
referencia "normal" de la misma hora del dia y estima que parte del ancho del
canal quedo tapada por agua nueva (0 % = nivel de base, 100 % = borde a borde).

Uso:
    python monitor.py                       # vigila todas las camaras en bucle
    python monitor.py --una-vez             # una sola ronda
    python monitor.py --cam laspiur         # solo una camara
    python monitor.py --recalibrar          # borra las referencias de las camaras elegidas
    python monitor.py --imagen f.jpg --cam X  # mide una imagen suelta (pruebas)
"""
import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np

BASE = Path(__file__).resolve().parent
DATOS = BASE / "datos"
ANCHO, ALTO = 1920, 1080


def _polilinea(pts, t):
    """Punto a fraccion t (0..1) del largo de una polilinea."""
    pts = np.array(pts, dtype=float)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    acum = np.concatenate([[0], np.cumsum(seg)])
    d = t * acum[-1]
    i = min(np.searchsorted(acum, d, side="right") - 1, len(seg) - 1)
    f = (d - acum[i]) / seg[i]
    return pts[i] + f * (pts[i + 1] - pts[i])


def _cortes_por_bordes(izq, der, ts):
    return [(tuple(_polilinea(izq, t).round().astype(int)), tuple(_polilinea(der, t).round().astype(int)))
            for t in ts]


# Cada camara: cortes transversales del canal en la imagen 1920x1080,
# de lejos a cerca, como (punto en la orilla izquierda, punto en la derecha).
CAMARAS = {
    "laspiur": {
        "nombre": "Sixto Laspiur y Av. Buenos Aires",
        "video": "DXpoL-oLaKo",
        "cortes": _cortes_por_bordes(
            [(748, 215), (800, 330), (860, 455), (930, 590)],
            [(1010, 215), (1250, 320), (1560, 440), (1890, 580)],
            [0.30, 0.50, 0.70, 0.90]),
        "agua": [(0.18, 0.45), (0.20, 0.50), (0.22, 0.50), (0.17, 0.46)],
    },
    "parque-de-mayo": {
        "nombre": "Parque de Mayo",
        "video": "HCNs0AIW1mU",
        "cortes": [((780, 470), (1450, 560)), ((620, 560), (1420, 680)),
                   ((430, 680), (1370, 800)), ((250, 820), (1310, 920))],
        # incluye las plantas flotantes, que se mueven y no son una crecida
        "agua": [(0.42, 0.93), (0.42, 0.85), (0.43, 0.82), (0.40, 0.75)],
    },
    "casanova": {
        "nombre": "Casanova y Fuerte Argentino",
        "video": "R5AjH5tHUvo",
        # Los extremos izquierdos arrancan en el pasto, a la derecha de la senda peatonal.
        "cortes": [((1040, 215), (1310, 215)), ((935, 270), (1380, 270)),
                   ((815, 330), (1440, 340)), ((705, 400), (1460, 420))],
        # incluye los juncos de la orilla, que se mueven con el viento
        "agua": [(0.45, 0.72), (0.30, 0.78), (0.38, 0.72), (0.33, 0.68)],
    },
    # Aca solo se ve el cauce y la orilla derecha: los cortes van del borde
    # izquierdo de la imagen hasta el filo del talud contra el pasto.
    "panama": {
        "nombre": "Panamá y 1° de Marzo",
        "video": "UG5Xw5Ph9ug",
        "cortes": [((20, 260), (560, 260)), ((10, 420), (470, 420)),
                   ((10, 580), (370, 580)), ((10, 760), (260, 760))],
        "agua": [(0.42, 0.80), (0.22, 0.78), (0.0, 0.45), (0.0, 0.35)],
    },
}
NOMBRES_CORTE = ["lejos", "medio", "medio", "cerca"]

PUNTOS = 240          # muestras por corte
PARCHE = 25           # lado del parche para comparar textura (px)
CORR_MIN = 0.35       # por debajo de esto el punto "cambio" (posible agua)
VAR_MIN = 12.0        # la referencia necesita algo de textura para opinar

UMBRALES = [(70, "CRITICO"), (40, "ALTO"), (15, "SUBIENDO"), (0, "normal")]
NIVEL_ALERTA = 15     # % a partir del cual avisa
NIVEL_REF = 10        # por debajo de esto la captura renueva la referencia
SIM_REF_MIN = 0.52    # menos parecida que esto a la referencia = imagen no confiable (obstruida o movida)
SIM_NUEVA_REF = 0.85  # para guardarse como referencia, la captura tiene que parecerse asi a la anterior
CONSIST_MIN = 0.70    # consistencia minima de los fotogramas de una captura para usarla como referencia
HUECO = 8             # puntos seguidos sin cambio que cortan el avance del agua (de 240 por linea)
HUECO_ORILLA = 14     # idem, pero pegado a la orilla de base
RECALIBRAR_TRAS = 3   # capturas seguidas no confiables pero estables -> se renueva la referencia
DIAS_CAPTURAS = 7


def url_video(cam):
    return "https://www.youtube.com/watch?v=" + CAMARAS[cam]["video"]


class Rutas:
    def __init__(self, cam):
        self.dir = DATOS / cam
        self.refs = self.dir / "referencias"
        self.capturas = self.dir / "capturas"
        self.registro = self.dir / "registro.csv"
        self.alertas = self.dir / "alertas.log"
        self.ultimo = self.dir / "ultimo.jpg"
        self.ultimo_json = self.dir / "ultimo.json"

    def crear(self):
        for d in (self.refs, self.capturas):
            d.mkdir(parents=True, exist_ok=True)


def migrar_datos_viejos():
    """La primera version guardaba la unica camara (laspiur) suelta en datos/."""
    viejo = DATOS / "registro.csv"
    destino = DATOS / "laspiur"
    if viejo.exists() and not (destino / "registro.csv").exists():
        destino.mkdir(parents=True, exist_ok=True)
        for nombre in ("referencias", "capturas", "registro.csv", "alertas.log", "ultimo.jpg"):
            p = DATOS / nombre
            if p.exists():
                shutil.move(str(p), str(destino / nombre))


_urls = {}


def url_stream(cam, forzar=False):
    u = _urls.get(cam)
    if not forzar and u and time.time() - u[1] < 3600:
        return u[0]
    r = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--extractor-args",
         "youtube:player_client=android", "-f", "96/95/best", "-g", url_video(cam)],
        capture_output=True, text=True, timeout=120)
    lineas = [l for l in r.stdout.splitlines() if l.startswith("http")]
    if not lineas:
        raise RuntimeError("yt-dlp no devolvio el stream: " + r.stderr.strip()[-300:])
    _urls[cam] = (lineas[0], time.time())
    return lineas[0]


def capturar(cam, n=5, cada=4):
    """Mediana de n fotogramas, uno cada `cada` segundos (unos 20 s en total).

    La mediana borra autos y personas que pasan. Devuelve (imagen, consistencia): la
    consistencia es la similitud minima entre cada fotograma y la mediana; si es baja,
    algo grande se movio delante de la camara durante la captura (un camion, por ejemplo).
    """
    for intento in (1, 2):
        try:
            url = url_stream(cam, forzar=(intento == 2))
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", url, "-vf", f"fps=1/{cada}",
                     "-frames:v", str(n), "-q:v", "2", str(Path(tmp) / "f_%02d.jpg")],
                    check=True, timeout=240, capture_output=True)
                cuadros = [cv2.imread(str(p)) for p in sorted(Path(tmp).glob("f_*.jpg"))]
            cuadros = [c if c.shape[:2] == (ALTO, ANCHO) else cv2.resize(c, (ANCHO, ALTO))
                       for c in cuadros if c is not None]
            if not cuadros:
                raise RuntimeError("ffmpeg no entrego fotogramas")
            img = np.median(np.stack(cuadros), axis=0).astype(np.uint8)
            consistencia = min(similitud(c, img) for c in cuadros) if len(cuadros) > 1 else 1.0
            return img, consistencia
        except Exception:
            if intento == 2:
                raise


def _huella(img):
    """Mapa de bordes chico y normalizado: sirve para comparar dos imagenes enteras."""
    g = cv2.cvtColor(cv2.resize(img, (320, 180), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
    m = cv2.GaussianBlur(np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)), (5, 5), 0)
    return (m - m.mean()) / (m.std() + 1e-6)


def similitud(a, b):
    """1 = misma escena; capturas normales seguidas dan 0,82-0,99; un camion tapando, ~0,4.

    Una crecida cambia solo el canal, una parte chica de la imagen, asi que casi no la baja.
    """
    return float((_huella(a) * _huella(b)).mean())


BASES = BASE / "bases"   # imagen canonica de cada camara: sobre ella se definieron las lineas


def alinear(img, objetivo):
    """Lleva `img` a la geometria de `objetivo` (misma camara, quiza corrida o con otro zoom).

    Las camaras se mueven un poco (viento, operador): sin esto, las lineas de medicion caen en
    otro lugar del canal y todo parece "cambio". Busca puntos en comun (ORB) y ajusta
    traslacion + giro + escala. Devuelve (imagen alineada, mascara valida, info) o
    (None, None, motivo) si no hay forma confiable de alinear.
    """
    esc = 0.5
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    prep = lambda x: clahe.apply(cv2.cvtColor(cv2.resize(x, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA),
                                              cv2.COLOR_BGR2GRAY))
    orb = cv2.ORB_create(4000)
    ka, da = orb.detectAndCompute(prep(img), None)
    kb, db = orb.detectAndCompute(prep(objetivo), None)
    if da is None or db is None or len(ka) < 50 or len(kb) < 50:
        return None, None, "pocos puntos de referencia"
    pares = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    buenos = [p[0] for p in pares if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(buenos) < 30:
        return None, None, f"pocas coincidencias ({len(buenos)})"
    pa = np.float32([ka[m.queryIdx].pt for m in buenos]) / esc
    pb = np.float32([kb[m.trainIdx].pt for m in buenos]) / esc
    M, dentro = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    n = 0 if dentro is None else int(dentro.sum())
    if M is None or n < 25:
        return None, None, f"ajuste debil ({n} puntos)"
    escala = float(np.hypot(M[0, 0], M[1, 0]))
    giro = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    dx, dy = float(M[0, 2]), float(M[1, 2])
    info = {"dx": round(dx, 1), "dy": round(dy, 1), "escala": round(escala, 3), "giro": round(giro, 2), "puntos": n}
    if not 0.85 < escala < 1.18 or abs(giro) > 6 or abs(dx) > 0.2 * ANCHO or abs(dy) > 0.2 * ALTO:
        return None, None, f"la camara se movio demasiado {info}"
    alineada = cv2.warpAffine(img, M, (ANCHO, ALTO), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    valida = cv2.warpAffine(np.full((ALTO, ANCHO), 255, np.uint8), M, (ANCHO, ALTO)) > 0
    return alineada, valida, info


def lineas_corte(cam):
    out = []
    for a, b in CAMARAS[cam]["cortes"]:
        a, b = np.array(a, float), np.array(b, float)
        s = np.linspace(0, 1, PUNTOS)[:, None]
        out.append((a + s * (b - a)).round().astype(int))
    return out


def _gris(img):
    return cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)


def medir(cam, img, ref, valida=None):
    """Devuelve (nivel %, [% por corte], [mascara de puntos donde avanzo el agua, por corte])."""
    a, b = _gris(img), _gris(ref)
    k = (PARCHE, PARCHE)
    ma, mb = cv2.blur(a, k), cv2.blur(b, k)
    va = cv2.blur(a * a, k) - ma * ma
    vb = cv2.blur(b * b, k) - mb * mb
    cov = cv2.blur(a * b, k) - ma * mb
    corr = cov / (np.sqrt(np.maximum(va, 0) * np.maximum(vb, 0)) + 1e-3)

    porcentajes, mascaras = [], []
    for pts, (w0, w1) in zip(lineas_corte(cam), CAMARAS[cam]["agua"]):
        x, y = pts[:, 0], pts[:, 1]
        valido = vb[y, x] > VAR_MIN
        if valida is not None:
            valido &= valida[y, x]
        cambio = (corr[y, x] < CORR_MIN) & valido
        # suavizado: un punto cuenta si la mayoria de sus vecinos tambien cambio
        suave = np.convolve(cambio.astype(float), np.ones(9) / 9, mode="same") > 0.5
        pct, avance = _avance(suave, valido, w0, w1)
        porcentajes.append(pct)
        mascaras.append(avance)
    return float(np.median(porcentajes)), porcentajes, mascaras


def _avance(cambio, valido, w0, w1):
    """Cuanto avanzo el agua desde sus orillas de base hacia lo seco.

    Una crecida ensancha el agua: lo seco que esta pegado a la orilla pasa a ser agua. Por eso
    solo cuentan los cambios contiguos a la orilla de base (se toleran huecos chicos). Lo que
    cambia dentro del agua (reflejos del cielo, plantas que flotan) o lejos de ella (sombras
    en el talud, alguien que pasa) no es una crecida y se ignora.
    Devuelve (% de lo seco cubierto, mascara de los puntos que cuentan).
    """
    n = len(cambio)
    i0, i1 = int(round(w0 * n)), int(round(w1 * n))
    mascara = np.zeros(n, bool)

    def recorrer(indices):
        ultimo, hueco = None, 0
        for j in indices:
            if cambio[j]:
                ultimo, hueco = j, 0
            elif valido[j]:
                hueco += 1
                # en la orilla misma se permite un hueco mayor (el borde del agua de base es aproximado)
                if hueco > (HUECO_ORILLA if ultimo is None else HUECO):
                    break
        return ultimo

    extremo_izq = recorrer(range(i0 - 1, -1, -1))
    extremo_der = recorrer(range(i1, n))
    cubierto = 0
    if extremo_izq is not None:
        mascara[extremo_izq:i0] = True
        cubierto += i0 - extremo_izq
    if extremo_der is not None:
        mascara[i1:extremo_der + 1] = True
        cubierto += extremo_der + 1 - i1
    seco = i0 + (n - i1)
    return (100.0 * cubierto / seco if seco else 0.0), mascara


def tramos(mascara):
    """Compacta una mascara de puntos en tramos [desde, hasta, cambio] (fracciones de la linea)."""
    m = [bool(x) for x in mascara]
    out, inicio = [], 0
    for i in range(1, len(m) + 1):
        if i == len(m) or m[i] != m[inicio]:
            out.append([round(inicio / len(m), 4), round(i / len(m), 4), int(m[inicio])])
            inicio = i
    return out


def estado(nivel):
    return next(nombre for minimo, nombre in UMBRALES if nivel >= minimo)


def anotar(cam, img, nivel, mascaras, cuando, nota=""):
    out = img.copy()
    for i, (pts, masc) in enumerate(zip(lineas_corte(cam), mascaras)):
        for (x, y), m in zip(pts, masc):
            cv2.circle(out, (int(x), int(y)), 3, (0, 0, 255) if m else (0, 220, 0), -1)
        # numero de la linea, en un circulo al comienzo
        x0, y0 = int(pts[0][0]), int(pts[0][1])
        cx, cy = max(x0 - 30, 24), y0
        cv2.circle(out, (cx, cy), 22, (0, 0, 0), -1)
        cv2.circle(out, (cx, cy), 22, (255, 255, 255), 2)
        cv2.putText(out, str(i + 1), (cx - 10, cy + 11), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    est = estado(nivel)
    color = (0, 200, 0) if est == "normal" else (0, 165, 255) if est == "SUBIENDO" else (0, 0, 255)
    texto = f"{cuando:%d/%m %H:%M}  subida sobre la base {nivel:.0f}%  {est} {nota}"
    cv2.rectangle(out, (0, ALTO - 60), (ANCHO, ALTO), (0, 0, 0), -1)
    cv2.putText(out, texto, (20, ALTO - 18), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)
    return out


def referencia(rutas, hora):
    """Referencia de esa hora; si falta, la de la hora mas cercana."""
    for d in range(13):
        for h in {(hora - d) % 24, (hora + d) % 24}:
            p = rutas.refs / f"ref_{h:02d}.jpg"
            if p.exists():
                return cv2.imread(str(p)), h
    return None, None


def avisar(rutas, texto):
    with rutas.alertas.open("a", encoding="utf-8") as f:
        f.write(texto + "\n")
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 400)
    except Exception:
        pass


def limpiar_viejas(cam):
    limite = datetime.now() - timedelta(days=DIAS_CAPTURAS)
    for p in Rutas(cam).capturas.glob("*.jpg"):
        if datetime.fromtimestamp(p.stat().st_mtime) < limite:
            p.unlink()


def ciclo(cam, previo):
    rutas = Rutas(cam)
    rutas.crear()
    ahora = datetime.now()
    img, consistencia = capturar(cam)
    propia = rutas.refs / f"ref_{ahora.hour:02d}.jpg"
    ref, hora_ref = referencia(rutas, ahora.hour)
    n = len(CAMARAS[cam]["cortes"])
    archivo_previa, archivo_calidad = rutas.dir / "previa.jpg", rutas.dir / "calidad.json"
    # Corrige pequenos movimientos de la camara llevando la captura a la geometria de la
    # referencia (o de la imagen base si todavia no hay referencia).
    base = cv2.imread(str(BASES / f"{cam}.jpg")) if (BASES / f"{cam}.jpg").exists() else None
    objetivo = ref if ref is not None else base
    valida, ajuste = None, None
    if objetivo is not None:
        alineada, valida, ajuste = alinear(img, objetivo)
        if alineada is not None:
            img = alineada
    previa = cv2.imread(str(archivo_previa)) if archivo_previa.exists() else None
    sim_previa = similitud(img, previa) if previa is not None else None
    try:
        calidad = json.loads(archivo_calidad.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        calidad = {"seguidas": 0}
    # Solo una captura "limpia" puede ser referencia: estable durante la captura y parecida
    # a la anterior (asi no queda de referencia un camion que justo pasaba delante).
    apta = consistencia >= CONSIST_MIN and (sim_previa is None or sim_previa >= SIM_NUEVA_REF)
    sin_dato = ([0.0] * n, [np.zeros(PUNTOS, bool)] * n)
    no_confiable = False

    if ref is None or (hora_ref != ahora.hour and previo < NIVEL_ALERTA):
        # Sin referencia para esta hora: se toma esta captura como "normal", si esta limpia.
        if apta:
            cv2.imwrite(str(propia), img)
            nota = "(calibrando)"
        else:
            nota = "(calibrando: se espera una imagen limpia)"
        calidad["crudo"] = 0.0
        nivel, (cortes, mascaras) = 0.0, sin_dato
    else:
        sim_ref = similitud(img, ref)
        if sim_ref < SIM_REF_MIN:
            # Cambio la escena entera (camion delante, camara movida, lente sucio): no se mide.
            no_confiable = True
            nivel, (cortes, mascaras) = 0.0, sin_dato
            nota = f"(imagen no confiable: se parece {sim_ref:.2f} a la referencia)"
            # Si la vista quedo estable (o la referencia era mala), tras varias capturas
            # limpias y parecidas entre si se toma la vista actual como referencia.
            calidad["seguidas"] = calidad.get("seguidas", 0) + 1 if apta else 0
            if calidad["seguidas"] >= RECALIBRAR_TRAS:
                cv2.imwrite(str(propia), img)
                calidad["seguidas"] = 0
                nota = "(referencia renovada: la anterior ya no coincidia con la vista)"
        else:
            calidad["seguidas"] = 0
            crudo, cortes, mascaras = medir(cam, img, ref, valida)
            # Una subida se informa recien cuando dos mediciones seguidas la ven: se toma el
            # menor de los dos valores (un reflejo o una sombra pasajera no se repite igual).
            nivel = min(crudo, calidad.get("crudo", crudo))
            calidad["crudo"] = crudo
            nota = "" if hora_ref == ahora.hour else f"(ref {hora_ref:02d} h)"
            if nivel < crudo - 1:
                nota = (nota + f" (lectura {crudo:.0f} % sin confirmar)").strip()
            if crudo < NIVEL_REF and hora_ref == ahora.hour and apta:
                cv2.imwrite(str(propia), img)   # acompana cambios lentos de luz/pasto
    cv2.imwrite(str(archivo_previa), cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA))
    archivo_calidad.write_text(json.dumps({**calidad, "consistencia": round(consistencia, 3),
                                           "sim_previa": None if sim_previa is None else round(sim_previa, 3),
                                           "ajuste": ajuste}),
                               encoding="utf-8")

    est = "NO_CONFIABLE" if no_confiable else estado(nivel)
    cv2.imwrite(str(rutas.ultimo), anotar(cam, img, nivel, mascaras, ahora, nota))
    # Tramos de cada linea (desde, hasta, cambio) para dibujarlos sobre el video en la web.
    rutas.ultimo_json.write_text(json.dumps({"fecha_hora": f"{ahora:%Y-%m-%d %H:%M}",
                                             "tramos": [tramos(m) for m in mascaras]}), encoding="utf-8")
    cv2.imwrite(str(rutas.capturas / f"{ahora:%Y%m%d_%H%M}.jpg"),
                cv2.resize(img, (960, 540)), [cv2.IMWRITE_JPEG_QUALITY, 80])
    nuevo = not rutas.registro.exists()
    with rutas.registro.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha_hora", "nivel_pct", "estado"] + [f"corte_{i + 1}" for i in range(n)] + ["nota"])
        w.writerow([f"{ahora:%Y-%m-%d %H:%M}", f"{nivel:.1f}", est] + [f"{c:.1f}" for c in cortes] + [nota])

    linea = f"{ahora:%d/%m %H:%M}  [{cam}] nivel {nivel:5.1f}%  {est} {nota}"
    print(linea, flush=True)
    # Avisa solo si dos mediciones seguidas superan el umbral (evita falsas alarmas).
    if nivel >= NIVEL_ALERTA and previo >= NIVEL_ALERTA:
        avisar(rutas, "ALERTA canal: " + linea)
        print("   >>> ALERTA: el agua esta subiendo", flush=True)
    return nivel


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cam", action="append", choices=list(CAMARAS), help="camara (se puede repetir)")
    ap.add_argument("--intervalo", type=float, default=5, help="minutos entre rondas")
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--recalibrar", action="store_true")
    ap.add_argument("--imagen", help="medir una imagen suelta contra la referencia de esta hora")
    args = ap.parse_args()
    camaras = args.cam or list(CAMARAS)
    migrar_datos_viejos()

    if args.recalibrar:
        for cam in camaras:
            for p in Rutas(cam).refs.glob("ref_*.jpg"):
                p.unlink()
        print("Referencias borradas:", ", ".join(camaras))

    if args.imagen:
        cam = camaras[0]
        img = cv2.resize(cv2.imread(args.imagen), (ANCHO, ALTO))
        ref, h = referencia(Rutas(cam), datetime.now().hour)
        if ref is None:
            sys.exit("No hay referencias todavia para esa camara: corre el monitor primero.")
        nivel, cortes, mascaras = medir(cam, img, ref)
        salida = str(Path(args.imagen).with_name(Path(args.imagen).stem + "_medido.jpg"))
        cv2.imwrite(salida, anotar(cam, img, nivel, mascaras, datetime.now(), f"(ref {h:02d} h)"))
        print(f"nivel {nivel:.1f}%  {estado(nivel)}  cortes {[round(c) for c in cortes]}  -> {salida}")
        return

    previos = {cam: 0.0 for cam in camaras}
    while True:
        for cam in camaras:
            try:
                previos[cam] = ciclo(cam, previos[cam])
            except Exception as e:
                print(f"{datetime.now():%d/%m %H:%M}  [{cam}] error: {e}", flush=True)
            limpiar_viejas(cam)
        if args.una_vez:
            break
        time.sleep(args.intervalo * 60)


if __name__ == "__main__":
    main()
