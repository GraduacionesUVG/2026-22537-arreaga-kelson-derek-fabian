"""
Cliente HTTP hacia la Meta Cloud API (envío/recepción de WhatsApp, Módulo B).

Requiere WHATSAPP_TOKEN y WHATSAPP_PHONE_NUMBER_ID (ver .env.example) --
sin esas dos variables, send_message lanza WhatsAppApiError con un mensaje
claro en vez de intentar una llamada que de todas formas fallaría.
"""

import os
import httpx

from src.configuration import settings

# WHATSAPP_API_BASE_URL solo se cambia en pruebas de carga, para
# apuntar a un Meta simulado; en cualquier otro ambiente queda el valor real.
GRAPH_API_BASE_URL = os.getenv("WHATSAPP_API_BASE_URL", "https://graph.facebook.com/v20.0").rstrip("/")


class WhatsAppApiError(Exception):
    """Cualquier respuesta no exitosa de la Graph API, o credenciales faltantes."""


def _require_credentials() -> tuple[str, str]:
    token = settings.whatsapp.token
    phone_number_id = settings.whatsapp.phone_number_id
    if not token or not phone_number_id:
        raise WhatsAppApiError(
            "Faltan WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID -- configúralos en .env antes de enviar mensajes."
        )
    return token, phone_number_id


async def send_message(to: str, payload: dict) -> dict:
    """POST directo a `/{phone_number_id}/messages` -- `payload` ya trae el
    cuerpo del mensaje según el formato de la Graph API (ver send_text)."""
    token, phone_number_id = _require_credentials()
    url = f"{GRAPH_API_BASE_URL}/{phone_number_id}/messages"
    body = {"messaging_product": "whatsapp", "to": to, **payload}

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
        raise WhatsAppApiError(f"Graph API respondió {response.status_code}: {response.text}")
    return response.json()


async def send_text(to: str, text: str) -> dict:
    return await send_message(to, {"type": "text", "text": {"body": text, "preview_url": False}})


def _template_text(value: str) -> str:
    """Meta rechaza parámetros de plantilla con saltos de línea, tabs o 4+
    espacios seguidos, y también vacíos."""
    value = " ".join(str(value).split())
    return value or "-"


async def send_template(
    to: str,
    name: str,
    language: str,
    body_params: list[str] | None = None,
    url_button_param: str | None = None,
) -> dict:
    """Mensaje de plantilla aprobada. `body_params` llena {{1}}, {{2}}... del
    cuerpo; `url_button_param` llena el {{1}} del botón de URL *dinámico*
    (un botón de URL estático no lleva parámetros)."""
    components = []
    if body_params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": _template_text(p)} for p in body_params]}
        )
    if url_button_param is not None:
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": url_button_param}],
            }
        )
    template = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components
    return await send_message(to, {"type": "template", "template": template})


async def download_media(media_id: str) -> bytes:
    """TODO: usado por el flujo de audios entrantes (webhook, no este módulo
    de salientes) -- se implementa junto con la transcripción real en
    src/api/v1/routes/webhooks.py."""
    raise NotImplementedError("Descarga de media entrante: pendiente junto con el webhook real de Meta.")
