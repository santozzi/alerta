"""Bot de Telegram de Bahia Alerta: avisa cuando algo cambia y responde comandos.

Configuracion (archivo .env, fuera de git):
    TELEGRAM_BOT_TOKEN  token que da @BotFather
    TELEGRAM_CHAT_ID    chat que recibe los avisos. Si queda vacio, se toma el
                        primer chat privado que le escriba al bot y se guarda en
                        datos/telegram_chat.json (los demas chats se ignoran).

    TELEGRAM_CANAL      (opcional) canal publico, p. ej. @bahiaalerta, donde el bot es administrador
    TELEGRAM_UMBRALES   (opcional) niveles de un canal que disparan aviso al subir (por defecto 15,40,70)

Avisos importantes, a tu chat y al canal (sin repetir mientras el estado no cambie):
    - el semaforo general sube o vuelve a la normalidad
    - un canal cambia de estado (Subiendo, Alto, Critico) o pasa a "sube rapido", con la foto
    - el SMN emite una alerta nueva para Bahia Blanca o las sierras
Ademas, aviso cuando un canal pasa 15, 40 o 70 % (una vez por subida; coinciden con los
estados Subiendo, Alto y Critico, asi que llega un solo mensaje por umbral).
Comandos: /estado, /foto <camara>, /camaras, /ayuda
"""
import html
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

API = "https://api.telegram.org/bot{token}/{metodo}"
RIESGO = ["Todo normal", "Atención", "Alerta", "Crítico"]
EMOJI_RIESGO = ["🟢", "🟡", "🟠", "🔴"]
ESTADOS = {"normal": 0, "SUBIENDO": 1, "ALTO": 2, "CRITICO": 3}
NOMBRE_ESTADO = {"normal": "Normal", "SUBIENDO": "Subiendo", "ALTO": "Alto", "CRITICO": "Crítico"}
EMOJI_SMN = {"amarilla": "🟨", "naranja": "🟧", "roja": "🟥"}
ESPERA_REPETIR = 30 * 60   # s minimos antes de repetir un aviso del mismo tipo para lo mismo
PIE_PUBLICO = ("\n\n<i>Bahía Alerta es experimental y depende de servicios externos. "
               "Ante una emergencia, seguí a Defensa Civil y al SMN.</i>")


class Bot:
    def __init__(self, token, chat_id, carpeta, obtener_resumen, ruta_foto, url_publica="", canal="",
                 umbrales=(15, 40, 70)):
        self.token = token
        self.chat_id = str(chat_id).strip() or None
        # Canal publico (opcional, apagado si queda vacio).
        self.canal = str(canal).strip() or None
        self.umbrales = sorted(umbrales)   # niveles de un canal que disparan aviso al subir
        self.archivo_chat = carpeta / "telegram_chat.json"
        self.archivo_estado = carpeta / "telegram_estado.json"
        self.obtener_resumen = obtener_resumen      # funcion -> dict de app.resumen_general()
        self.ruta_foto = ruta_foto                  # funcion(cam) -> Path de la ultima captura
        self.url_publica = url_publica
        self.offset = 0
        self.ultimo_error = None
        if not self.chat_id and self.archivo_chat.exists():
            self.chat_id = json.loads(self.archivo_chat.read_text(encoding="utf-8")).get("chat_id")
        self.estado = {}
        if self.archivo_estado.exists():
            try:
                self.estado = json.loads(self.archivo_estado.read_text(encoding="utf-8"))
            except ValueError:
                self.estado = {}

    # ---------------------------------------------------------------- API
    def _llamar(self, metodo, datos=None, archivos=None, timeout=30):
        url = API.format(token=self.token, metodo=metodo)
        if archivos:
            limite = uuid.uuid4().hex
            partes = []
            for k, v in (datos or {}).items():
                partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k, (nombre, contenido, tipo) in archivos.items():
                partes.append(f'--{limite}\r\nContent-Disposition: form-data; name="{k}"; filename="{nombre}"\r\n'
                              f'Content-Type: {tipo}\r\n\r\n'.encode() + contenido + b"\r\n")
            cuerpo = b"".join(partes) + f"--{limite}--\r\n".encode()
            req = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
        else:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(datos or {}).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Nunca se muestra la URL: lleva el token.
            try:
                detalle = json.load(e).get("description", "")
            except ValueError:
                detalle = ""
            raise RuntimeError(f"Telegram {metodo}: HTTP {e.code} {detalle}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Telegram {metodo}: sin conexion ({e.reason})") from None

    def enviar(self, texto, chat_id=None):
        chat = chat_id or self.chat_id
        if not chat:
            return
        self._llamar("sendMessage", {"chat_id": chat, "text": texto[:4000], "parse_mode": "HTML",
                                     "disable_web_page_preview": "true"})

    def enviar_foto(self, ruta, texto, chat_id=None):
        chat = chat_id or self.chat_id
        if not chat:
            return
        if not ruta or not ruta.exists():
            return self.enviar(texto, chat)
        self._llamar("sendPhoto", {"chat_id": chat, "caption": texto[:1000], "parse_mode": "HTML"},
                     {"photo": ("captura.jpg", ruta.read_bytes(), "image/jpeg")}, timeout=60)

    def publicar(self, texto, foto=None):
        """Aviso importante: a tu chat y, si hay, al canal publico (con el aviso de responsabilidad)."""
        self.enviar_foto(foto, texto) if foto else self.enviar(texto)
        if self.canal:
            try:
                pie = PIE_PUBLICO + (f"\n{self.url_publica}" if self.url_publica else "")
                if foto:
                    self.enviar_foto(foto, texto[: 1000 - len(pie)] + pie, self.canal)
                else:
                    self.enviar(texto + pie, self.canal)
            except Exception as e:
                print("telegram canal:", e, flush=True)

    def verificar_canal(self):
        if not self.canal:
            return
        try:
            yo = self._llamar("getMe")["result"]["id"]
            m = self._llamar("getChatMember", {"chat_id": self.canal, "user_id": yo})["result"]
            ok = m.get("status") == "administrator" and m.get("can_post_messages", True)
            print(f"telegram: canal {self.canal} " + ("listo" if ok else "encontrado, pero el bot no es administrador con permiso para publicar"), flush=True)
        except Exception as e:
            print(f"telegram: no se pudo usar el canal {self.canal}: {e}", flush=True)

    # ------------------------------------------------------------ textos
    def _texto_estado(self, r):
        rg = r["riesgo"]
        lineas = [f"{EMOJI_RIESGO[rg['nivel']]} <b>Bahía Alerta: {RIESGO[rg['nivel']]}</b>"]
        lineas += [f"• {html.escape(m)}" for m in rg["motivos"]] or ["• Ningún canal sube y no hay lluvia fuerte prevista."]
        lineas.append("")
        for c in r["camaras"]:
            a, t = c["actual"], c["tendencia"] or {}
            if not a:
                lineas.append(f"• {html.escape(c['nombre'])}: sin datos")
                continue
            ritmo = f", {t['v']:+.0f} %/h" if t.get("v") is not None else ""
            lineas.append(f"• {html.escape(c['nombre'])}: {a['nivel']:.0f}% {NOMBRE_ESTADO.get(a['estado'], a['estado'])}{ritmo}")
        k, m = r.get("clima"), r.get("marea")
        if k:
            lineas.append(f"\n🌧 Lluvia 6 h: {k['lluvia_6h']:.1f} mm · prevista 6 h: {k['prevista_6h']:.1f} mm"
                          f"\n💨 Viento {k['viento']:.0f} km/h (ráfagas {k['rafagas']:.0f})")
        if m and m.get("proxima_pleamar"):
            p = m["proxima_pleamar"]
            lineas.append(f"🌊 Próxima pleamar {p['t'][11:16]} ({p['h']:.2f} m)")
        if self.url_publica:
            lineas.append(f"\n{self.url_publica}")
        return "\n".join(lineas)

    # ------------------------------------------------------------ avisos
    def _guardar(self):
        self.archivo_estado.write_text(json.dumps(self.estado), encoding="utf-8")

    def _puede(self, clave):
        return time.time() - self.estado.get("enviado", {}).get(clave, 0) >= ESPERA_REPETIR

    def _marcar(self, clave):
        self.estado.setdefault("enviado", {})[clave] = time.time()

    def revisar(self):
        """Compara el estado actual con el anterior y manda los avisos que correspondan."""
        if not self.chat_id:
            return
        r = self.obtener_resumen()
        primera = "riesgo" not in self.estado
        nivel = r["riesgo"]["nivel"]

        # Semaforo general
        anterior = self.estado.get("riesgo", 0)
        if primera:
            self.enviar("✅ <b>Bahía Alerta conectado.</b> Te voy a avisar cuando cambie algo.\n\n" + self._texto_estado(r))
        elif nivel > anterior and self._puede(f"riesgo{nivel}"):
            self.publicar(f"⚠️ El semáforo subió a <b>{RIESGO[nivel]}</b>.\n\n" + self._texto_estado(r))
            self._marcar(f"riesgo{nivel}")
        elif nivel == 0 and anterior > 0:
            self.publicar("✅ <b>Todo volvió a la normalidad.</b>\n\n" + self._texto_estado(r))
        self.estado["riesgo"] = nivel

        # Cada canal
        previos = self.estado.setdefault("camaras", {})
        for c in r["camaras"]:
            a, t = c["actual"], c["tendencia"] or {}
            if not a:
                continue
            est, rit = ESTADOS.get(a["estado"], 0), t.get("etiqueta")
            p = previos.get(c["id"], {"estado": 0, "ritmo": None})
            motivo = None
            if est > p["estado"]:
                motivo = f"pasó a <b>{NOMBRE_ESTADO.get(a['estado'])}</b>"
            elif rit == "sube rápido" and p.get("ritmo") != "sube rápido":
                motivo = "<b>sube rápido</b>"
            ritmo = f" ({t['v']:+.0f} %/h)" if t.get("v") is not None else ""
            eta = t.get("eta")
            extra = f"\nA este ritmo llega a {eta['umbral']} % en unos {eta['minutos']} min." if eta else ""
            avisado_estado = False
            if motivo and not primera and self._puede(f"{c['id']}:{est}:{rit}"):
                self.publicar(f"🌊 <b>{html.escape(c['nombre'])}</b> {motivo}: {a['nivel']:.0f}%{ritmo}.{extra}",
                              self.ruta_foto(c["id"]))
                self._marcar(f"{c['id']}:{est}:{rit}")
                avisado_estado = True

            # Aviso al pasar cada umbral (15, 40 y 70 % por defecto), una sola vez por subida.
            # Histeresis: el umbral se "rearma" recien cuando el nivel baja 5 puntos por debajo,
            # asi el ruido alrededor del umbral (14 % <-> 16 %) no repite avisos.
            escalon = p.get("escalon", 0)
            actual = max((u for u in self.umbrales if a["nivel"] >= u), default=0)
            if actual > escalon:
                if not primera and not avisado_estado:
                    self.enviar_foto(self.ruta_foto(c["id"]),
                                     f"📈 <b>{html.escape(c['nombre'])}</b> pasó el {actual} %: está en {a['nivel']:.0f}%{ritmo}.{extra}")
                escalon = actual
            elif escalon and a["nivel"] <= escalon - 5:
                escalon = max((u for u in self.umbrales if a["nivel"] >= u), default=0)
            previos[c["id"]] = {"estado": est, "ritmo": rit, "escalon": escalon}

        # Alertas nuevas del SMN
        s = r.get("smn") or {}
        if s.get("completo"):
            vistas = set(self.estado.get("smn", []))
            actuales = []
            for a in s.get("alertas") or []:
                clave = f"{a['evento']}|{a['color']}|{a['desde'][:16]}|{a['hasta'][:16]}"
                actuales.append(clave)
                if clave not in vistas and not primera:
                    cuando = "vigente ahora" if a["vigente"] else f"desde {a['desde'][8:10]}/{a['desde'][5:7]} {a['desde'][11:16]}"
                    self.publicar(f"{EMOJI_SMN.get(a['color'], '⚠️')} <b>SMN: alerta {a['color']} por {html.escape(a['evento'].lower())}</b>\n"
                                f"{html.escape(', '.join(a['lugares']))} · {cuando} hasta {a['hasta'][11:16]} del {a['hasta'][8:10]}/{a['hasta'][5:7]}\n\n"
                                f"{html.escape(a['descripcion'])[:1500]}")
            self.estado["smn"] = actuales
        self._guardar()

    # ----------------------------------------------------------- comandos
    def _atender(self, msg):
        chat = msg.get("chat", {})
        cid, texto = str(chat.get("id", "")), (msg.get("text") or "").strip()
        if not self.chat_id and chat.get("type") == "private":
            # Primer chat privado que escribe: queda como destinatario.
            self.chat_id = cid
            self.archivo_chat.write_text(json.dumps({"chat_id": cid, "desde": datetime.now().isoformat()}), encoding="utf-8")
            print(f"telegram: chat registrado ({chat.get('first_name', '')})", flush=True)
        if cid != self.chat_id:
            return   # otros chats se ignoran
        partes = texto.split()
        cmd = partes[0].split("@")[0].lower() if partes else ""
        r = self.obtener_resumen()
        if cmd in ("/estado", "/start"):
            self.enviar(self._texto_estado(r), cid)
        elif cmd == "/camaras":
            self.enviar("Cámaras: " + ", ".join(f"<code>{c['id']}</code> ({html.escape(c['nombre'])})" for c in r["camaras"])
                        + "\nUsá /foto seguido del nombre, por ejemplo /foto laspiur", cid)
        elif cmd == "/foto":
            buscado = " ".join(partes[1:]).lower()
            cam = next((c for c in r["camaras"] if buscado and (buscado in c["id"] or buscado in c["nombre"].lower())), None)
            if not cam:
                self.enviar("¿Qué cámara? Probá con /camaras para ver los nombres.", cid)
            else:
                a = cam["actual"]
                self.enviar_foto(self.ruta_foto(cam["id"]),
                                 f"📷 <b>{html.escape(cam['nombre'])}</b>: {a['nivel']:.0f}% "
                                 f"{NOMBRE_ESTADO.get(a['estado'], '')} (medido {a['fecha_hora'][11:]})" if a else cam["nombre"], cid)
        else:
            self.enviar("Comandos: /estado, /camaras, /foto &lt;cámara&gt;", cid)

    def escuchar(self):
        """Recibe mensajes por long polling."""
        while True:
            try:
                r = self._llamar("getUpdates", {"offset": self.offset, "timeout": 50, "allowed_updates": '["message"]'}, timeout=70)
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1
                    if "message" in u:
                        try:
                            self._atender(u["message"])
                        except Exception as e:
                            print("telegram comando:", e, flush=True)
                self.ultimo_error = None
            except Exception as e:
                self.ultimo_error = str(e)
                print("telegram escucha:", e, flush=True)
                time.sleep(30)   # p. ej. 409 si otro servidor usa el mismo bot

    def vigilar(self, cada=60):
        while True:
            try:
                self.revisar()
            except Exception as e:
                self.ultimo_error = str(e)
                print("telegram aviso:", e, flush=True)
            time.sleep(cada)

    def arrancar(self):
        self.verificar_canal()
        threading.Thread(target=self.escuchar, daemon=True).start()
        threading.Thread(target=self.vigilar, daemon=True).start()


def desde_entorno(carpeta, obtener_resumen, ruta_foto, url_publica=""):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    try:
        umbrales = [int(x) for x in os.environ.get("TELEGRAM_UMBRALES", "15,40,70").split(",") if x.strip()]
    except ValueError:
        umbrales = []
    umbrales = [u for u in umbrales if 0 < u <= 100] or [15, 40, 70]
    return Bot(token, os.environ.get("TELEGRAM_CHAT_ID", ""), carpeta, obtener_resumen, ruta_foto, url_publica,
               os.environ.get("TELEGRAM_CANAL", ""), umbrales)
