"""Ingreso al panel de administracion: clave, sesiones y freno a intentos repetidos.

La clave se toma de ADMIN_CLAVE en el .env. Si no esta (o tiene menos de 8
caracteres), el panel queda desactivado. Las sesiones viven en memoria: al
reiniciar el servidor hay que volver a ingresar.
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

COOKIE = "ba_admin"
DURACION = 12 * 3600          # segundos que dura una sesion
VENTANA = 15 * 60             # ventana para contar intentos fallidos
MAX_POR_IP, MAX_TOTAL = 5, 30

_sesiones = {}                # token -> vencimiento
_fallos = []                  # [(momento, ip)]
_candado = threading.Lock()


def clave_configurada():
    return len(os.environ.get("ADMIN_CLAVE", "")) >= 8


def _limpiar(ahora):
    for t in [t for t, v in _sesiones.items() if v < ahora]:
        del _sesiones[t]
    _fallos[:] = [f for f in _fallos if f[0] > ahora - VENTANA]


def bloqueado(ip):
    """Segundos que faltan para poder volver a intentar, o 0."""
    with _candado:
        ahora = time.time()
        _limpiar(ahora)
        propios = [t for t, i in _fallos if i == ip]
        if len(propios) >= MAX_POR_IP:
            return int(propios[0] + VENTANA - ahora) + 1
        if len(_fallos) >= MAX_TOTAL:
            return int(_fallos[0][0] + VENTANA - ahora) + 1
        return 0


def ingresar(clave, ip):
    """Devuelve un token de sesion si la clave es correcta, o None."""
    if not clave_configurada() or not isinstance(clave, str):
        return None
    correcta = os.environ["ADMIN_CLAVE"]
    # Se comparan los hashes en tiempo constante para no filtrar nada por el tiempo de respuesta.
    ok = hmac.compare_digest(hashlib.sha256(clave.encode()).digest(), hashlib.sha256(correcta.encode()).digest())
    with _candado:
        ahora = time.time()
        _limpiar(ahora)
        if not ok:
            _fallos.append((ahora, ip))
            return None
        token = secrets.token_urlsafe(32)
        _sesiones[token] = ahora + DURACION
        return token


def valida(token):
    with _candado:
        ahora = time.time()
        _limpiar(ahora)
        return bool(token) and token in _sesiones


def salir(token):
    with _candado:
        _sesiones.pop(token, None)
