"""
Webhook de WhatsApp 

Recibe los eventos de la WhatsApp Cloud API, extrae los mensajes de texto y
audio (transcribiéndolos con Whisper), consulta POST /auth/whatsapp/check
para decidir si el estudiante ya puede chatear o hay que mandarlo a
registrarse, y si ya está verificado lo
enruta al Orquestador LLM (orchestator.py) y devuelve la respuesta por la
Cloud API de Meta.

Uso:
    uvicorn webhook:app --reload --port 3000
"""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Response
from openai import OpenAIError

from backend_client import BackendError, check_whatsapp_registration, report_conversation_metrics
from message_limits import (
    LIMIT_REACHED_MESSAGE,
    limit_reached,
    record_sent,
    should_notify_limit,
)
from metrics import meta_timestamp
from orchestator import get_session, split_into_whatsapp_messages
from transcription import transcribir_audio

# La consola de Windows suele usar cp1252, que no puede imprimir emojis;
# el agente los usa en sus respuestas (ver SYSTEM_PROMPT), así que forzamos UTF-8.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
# App Secret de la app de Meta (Meta for Developers > Configuración > Básica). Con él se
# comprueba la firma X-Hub-Signature-256 de cada POST: sin firma válida, el mensaje no es
# de Meta y se descarta. Sin definir, NO se verifica (solo para desarrollo).
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
if not WHATSAPP_APP_SECRET:
    print("[webhook] AVISO: WHATSAPP_APP_SECRET no está definido; no se verifica la firma de Meta.")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
GRAPH_API_VERSION = "v20.0"

TEMPLATES_ENABLED = os.getenv("WHATSAPP_TEMPLATES_ENABLED", "false").lower() == "true"
TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "es")
TEMPLATE_REGISTER = os.getenv("WHATSAPP_TEMPLATE_REGISTER", "registro_usuario")

AUDIO_ERROR_MESSAGE = "No pude escuchar tu audio 🙁 ¿Me lo puedes escribir?"

# Meta puede mandar el mismo mensaje más de una vez 
_MAX_IDS_RECORDADOS = 1000
_ids_procesados: OrderedDict[str, None] = OrderedDict()
_ids_lock = threading.Lock()

# Los mensajes se procesan en hilos (BackgroundTasks): un lock por número para
# que dos mensajes seguidos del mismo estudiante no toquen su sesión a la vez.
_user_locks: dict[str, threading.Lock] = {}

LOG_MESSAGE_CONTENT = os.getenv("LOG_MESSAGE_CONTENT", "false").lower() == "true"


def _mask(wa_id: str | None) -> str:
    return f"...{wa_id[-4:]}" if wa_id else "?"


def _contenido(texto: str) -> str:
    return repr(texto) if LOG_MESSAGE_CONTENT else f"({len(texto)} caracteres)"


@app.get("/health")
def health():
    """Para el health check del hosting (Render) """
    return {"status": "ok"}


@app.get("/")
def verify_webhook(request: Request):
    """Verificación inicial del webhook."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("WEBHOOK VERIFIED")
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


def _firma_valida(raw_body: bytes, signature_header: str | None) -> bool:
    """X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(App Secret, cuerpo crudo)."""
    if not WHATSAPP_APP_SECRET:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    esperado = hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header.removeprefix("sha256="), esperado)


@app.post("/")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    if not _firma_valida(raw_body, request.headers.get("x-hub-signature-256")):
        print("[webhook] POST rechazado: firma X-Hub-Signature-256 ausente o inválida.")
        return Response(status_code=403)
    try:
        body = json.loads(raw_body)
    except ValueError:
        return Response(status_code=400)
    print(f"\nWebhook received {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "messages":
                background_tasks.add_task(_handle_messages_change, change.get("value", {}))

    return Response(status_code=200)


# Meta manda por el mismo webhook los estados de CADA mensaje saliente (también los que manda el backend
# con la misma línea): sent / delivered / read / failed. Un "failed" trae el motivo real por el que no llegó.
LOG_WA_STATUSES = os.getenv("LOG_WA_STATUSES", "").lower() in ("1", "true")


def _log_statuses(value: dict) -> None:
    for status in value.get("statuses", []) or []:
        estado = status.get("status")
        destino = _mask(status.get("recipient_id"))
        if estado == "failed":
            for error in status.get("errors") or [{}]:
                detalle = (error.get("error_data") or {}).get("details") or error.get("message") or ""
                print(
                    f"[webhook] ENTREGA FALLIDA a {destino} (mensaje {status.get('id')}): "
                    f"código {error.get('code')} - {error.get('title')} - {detalle}"
                )
        elif LOG_WA_STATUSES:
            print(f"[webhook] estado '{estado}' a {destino} (mensaje {status.get('id')})")


def _handle_messages_change(value: dict) -> None:
    _log_statuses(value)
    for message in value.get("messages", []):
        message_id = message.get("id")
        if _ya_procesado(message_id):
            print(f"[webhook] Mensaje duplicado {message_id}, se ignora.")
            continue

        with _user_locks.setdefault(message.get("from"), threading.Lock()):
            _procesar_mensaje(message)


def _ya_procesado(message_id: str | None) -> bool:
    """Marca el wamid como procesado; True si ya se había visto antes."""
    if not message_id:
        return False
    with _ids_lock:
        if message_id in _ids_procesados:
            return True
        _ids_procesados[message_id] = None
        if len(_ids_procesados) > _MAX_IDS_RECORDADOS:
            _ids_procesados.popitem(last=False)
        return False


def _procesar_mensaje(message: dict) -> None:
    received_at = time.perf_counter()
    wa_id = message.get("from")
    msg_type = message.get("type")

    texto = _extraer_texto(message, msg_type)
    audio_ms = (time.perf_counter() - received_at) * 1000 if msg_type == "audio" else 0.0
    if not texto:
        if msg_type == "audio":
            if not limit_reached(wa_id):
                _enviar_mensaje_whatsapp(wa_id, AUDIO_ERROR_MESSAGE)
        else:
            print(f"[webhook] Tipo de mensaje no soportado todavía: {msg_type}")
        return

    print(f"[webhook] Mensaje de {_mask(wa_id)} [{message.get('id')}] {msg_type}: {_contenido(texto)}")

    if limit_reached(wa_id):
        print(f"[webhook] {_mask(wa_id)} llegó al tope diario de mensajes, se ignora el mensaje.")
        if should_notify_limit(wa_id):
            _enviar_mensaje_whatsapp(wa_id, LIMIT_REACHED_MESSAGE)
        return

    try:
        check = check_whatsapp_registration(wa_id)
    except BackendError as exc:
        print(f"[webhook] No se pudo consultar el registro de {_mask(wa_id)}: {exc.detail}")
        _enviar_mensaje_whatsapp(wa_id, "Tuvimos un problema para revisar tu registro. Intenta de nuevo en un momento.")
        return
    except httpx.HTTPError as exc:
        print(f"[webhook] El backend no respondió a tiempo consultando {_mask(wa_id)}: {exc}")
        _enviar_mensaje_whatsapp(wa_id, "Tuvimos un problema técnico. Intenta de nuevo en un momento.")
        return

    if check["status"] != "verified":
        respuesta = check["message"]
        login_url = check.get("login_url")
        if login_url:
            if TEMPLATES_ENABLED and _enviar_plantilla_registro(wa_id, respuesta):
                return
            separator = "&" if "?" in login_url else "?"
            login_url = f"{login_url}{separator}wa={wa_id}"
            respuesta += f"\n{login_url}"
        _enviar_mensaje_whatsapp(wa_id, respuesta)
        return

    # La sesión es por cliente.
    session = get_session(check["student_id"], check.get("full_name"))
    session.metrics.begin_turn(meta_timestamp(message.get("timestamp")), msg_type, audio_ms)
    session.add_user_message(texto)
    respuesta = session.run_turn()
    session.metrics.end_turn(since=received_at)
    print(f"[webhook] Agente -> {_mask(wa_id)}: {_contenido(respuesta)}")

    for i, mensaje in enumerate(split_into_whatsapp_messages(respuesta) or [respuesta]):
        if i > 0:
            time.sleep(0.6)
        _enviar_mensaje_whatsapp(wa_id, mensaje)

    _reportar_metricas(session)


def _reportar_metricas(session) -> None:
    """Tras contestar al estudiante: si este turno creó un pedido, guarda los tiempos y reinicia el conteo."""
    payload = session.metrics.payload()
    if payload is None:
        return
    report_conversation_metrics(payload)
    session.metrics.reset()


def _extraer_texto(message: dict, msg_type: str) -> str | None:
    if msg_type == "text":
        return message.get("text", {}).get("body")

    if msg_type == "audio":
        return _transcribir_mensaje_audio(message.get("audio", {}))

    return None


def _enviar_mensaje_whatsapp(wa_id: str, texto: str) -> None:
    """Responde al estudiante por WhatsApp (Meta Cloud API, Send Message)."""
    _post_whatsapp(wa_id, {"type": "text", "text": {"body": texto}})


def _enviar_plantilla_registro(wa_id: str, mensaje: str) -> bool:
    """Manda la plantilla de registro con el botón dinámico."""

    cuerpo = " ".join(mensaje.split()) or "-"
    return _post_whatsapp(
        wa_id,
        {
            "type": "template",
            "template": {
                "name": TEMPLATE_REGISTER,
                "language": {"code": TEMPLATE_LANGUAGE},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": cuerpo}]},
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": wa_id}],
                    },
                ],
            },
        },
    )


def _post_whatsapp(wa_id: str, contenido: dict) -> bool:
    """POST a la Graph API (texto o plantilla). Devuelve si Meta lo aceptó."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print("[webhook] No se pudo enviar la respuesta: falta WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID en el .env.")
        return False

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {"messaging_product": "whatsapp", "to": wa_id, **contenido}
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=15.0)
        response.raise_for_status()
        record_sent(wa_id)
        return True
    except httpx.HTTPStatusError as exc:
        print(f"[webhook] Meta rechazó el envío a {_mask(wa_id)}: {exc.response.status_code} {exc.response.text}")
    except httpx.HTTPError as exc:
        print(f"[webhook] Error de red enviando el mensaje a {_mask(wa_id)}: {exc}")
    return False


def _transcribir_mensaje_audio(audio: dict) -> str | None:
    url = audio.get("url")
    if not url or not WHATSAPP_TOKEN:
        print("[webhook] No se pudo descargar el audio: falta 'url' o WHATSAPP_TOKEN.")
        return None

    # Cualquier falla acá se traga (devuelve None): si el webhook responde
    # 500, Meta reintenta el mismo evento una y otra vez.
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        response = httpx.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[webhook] No se pudo descargar el audio: {exc}")
        return None

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_file:
        tmp_file.write(response.content)
        tmp_path = tmp_file.name

    try:
        return transcribir_audio(tmp_path)
    except OpenAIError as exc:
        print(f"[webhook] Whisper no pudo transcribir el audio: {exc}")
        return None
    finally:
        os.remove(tmp_path)
