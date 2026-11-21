"""
Salud de OpenAI y de la WhatsApp Cloud API (las dos dependencias externas del
agente), en dos capas:

1. Plataforma: lo que reportan sus páginas de estado públicas (sin credenciales).
2. Credenciales: un sondeo barato con nuestras llaves (valida la llave/token
   sin gastar tokens ni mandar mensajes).

Nunca lanza: cualquier fallo de red o de formato se devuelve como `unknown`/`down`
dentro del resultado, para que el panel siempre pueda mostrar algo.
"""

import asyncio
import time
from datetime import datetime, timezone

import httpx

from src.configuration import settings
from src.integrations.whatsapp_client import GRAPH_API_BASE_URL

OPENAI_STATUS_URL = "https://status.openai.com/api/v2/summary.json"
OPENAI_API_URL = "https://api.openai.com/v1"
# status.openai.com agrupa por componente; solo nos importan los que usa el agente.
OPENAI_COMPONENTS = ("Chat Completions", "Audio")

# metastatus.com/data/orgs.json es el JSON que consume la propia página de Meta
# (no es una API documentada: puede cambiar de forma). Por eso todo se lee con
# .get() y, si no se entiende, el resultado es `unknown` en vez de un error.
META_STATUS_URL = "https://metastatus.com/data/orgs.json"
META_ORG_ID = "whatsapp-business-api"
META_SERVICES = ("Cloud API",)

CACHE_TTL_SECONDS = 60
TIMEOUT = 8.0

_SEVERITY = {"operational": 0, "unknown": 1, "degraded": 2, "down": 3}
_cache: dict[str, tuple[float, dict]] = {}


def worst(*statuses: str | None) -> str:
    """El peor de varios estados (None se ignora). `unknown` pesa menos que degraded."""
    return max((s for s in statuses if s), key=lambda s: _SEVERITY.get(s, 1), default="unknown")


def _result(status: str, detail: str | None = None, started: float | None = None, data: dict | None = None) -> dict:
    latency = int((time.monotonic() - started) * 1000) if started is not None else None
    return {"status": status, "detail": detail, "latency_ms": latency, "data": data}


# --- OpenAI -----------------------------------------------------------------

_OPENAI_COMPONENT_STATUS = {
    "operational": "operational",
    "degraded_performance": "degraded",
    "under_maintenance": "degraded",
    "partial_outage": "degraded",
    "major_outage": "down",
}


async def openai_platform_status(client: httpx.AsyncClient) -> dict:
    started = time.monotonic()
    try:
        response = await client.get(OPENAI_STATUS_URL)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _result("unknown", f"No se pudo leer status.openai.com: {type(exc).__name__}", started)

    components = {c.get("name"): c.get("status") for c in body.get("components", []) if isinstance(c, dict)}
    relevant = {name: components[name] for name in OPENAI_COMPONENTS if name in components}
    if not relevant:
        return _result("unknown", "status.openai.com no trae los componentes esperados.", started)
    status = worst(*(_OPENAI_COMPONENT_STATUS.get(s, "unknown") for s in relevant.values()))
    incidents = [i.get("name") for i in body.get("incidents", []) if isinstance(i, dict) and i.get("name")]
    detail = None if status == "operational" else "; ".join(incidents) or "Componente afectado."
    return _result(status, detail, started, {"components": relevant, "incidents": incidents})


async def openai_credentials_check(client: httpx.AsyncClient) -> dict:
    """GET /v1/models/{modelo}: valida la llave y que tengamos el modelo, sin gastar
    tokens. NO detecta falta de saldo (eso solo sale en una llamada real: 429
    `insufficient_quota`)."""
    started = time.monotonic()
    key = settings.openai.api_key
    if not key:
        return _result("down", "Falta OPENAI_API_KEY en el backend.")
    try:
        response = await client.get(
            f"{OPENAI_API_URL}/models/{settings.openai.model}", headers={"Authorization": f"Bearer {key}"}
        )
    except httpx.HTTPError as exc:
        return _result("unknown", f"No se pudo contactar a OpenAI: {type(exc).__name__}", started)
    if response.status_code == 200:
        return _result("operational", None, started, {"model": settings.openai.model})
    if response.status_code == 401:
        return _result("down", "La llave de OpenAI es inválida o fue revocada.", started)
    if response.status_code == 404:
        return _result("down", f"La llave no tiene acceso al modelo '{settings.openai.model}'.", started)
    if response.status_code == 429:
        return _result("degraded", "OpenAI limitó la consulta (429): revisa saldo y límites.", started)
    return _result("degraded", f"OpenAI respondió {response.status_code}.", started)


# --- WhatsApp ---------------------------------------------------------------


async def whatsapp_platform_status(client: httpx.AsyncClient) -> dict:
    started = time.monotonic()
    try:
        response = await client.get(META_STATUS_URL)
        response.raise_for_status()
        orgs = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return _result("unknown", f"No se pudo leer metastatus.com: {type(exc).__name__}", started)

    org = None
    if isinstance(orgs, list):
        org = next((o for o in orgs if isinstance(o, dict) and o.get("id") == META_ORG_ID), None)
    services = {s.get("name"): s.get("status") for s in (org or {}).get("services", []) if isinstance(s, dict)}
    relevant = {name: services[name] for name in META_SERVICES if name in services}
    if not relevant:
        return _result("unknown", "metastatus.com no trae el servicio Cloud API (¿cambió el formato?).", started)
    # "No known issues" es lo único que Meta usa para "todo bien"; cualquier otro
    # texto es un incidente en curso.
    affected = {n: s for n, s in relevant.items() if str(s).strip().lower() != "no known issues"}
    status = "degraded" if affected else "operational"
    return _result(status, "; ".join(f"{n}: {s}" for n, s in affected.items()) or None, started, {"services": relevant})


async def whatsapp_credentials_check(client: httpx.AsyncClient) -> dict:
    """GET /{PHONE_NUMBER_ID}: detecta token vencido/inválido (error 190; el token
    temporal de pruebas de Meta caduca cada 24 h) y trae la calidad del número."""
    started = time.monotonic()
    token, phone_id = settings.whatsapp.token, settings.whatsapp.phone_number_id
    if not token or not phone_id:
        return _result("down", "Faltan WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID en el backend.")
    try:
        response = await client.get(
            f"{GRAPH_API_BASE_URL}/{phone_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating,status"},
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        return _result("unknown", f"No se pudo contactar a la Graph API: {type(exc).__name__}", started)

    if response.status_code == 200:
        body = response.json()
        data = {k: body.get(k) for k in ("display_phone_number", "verified_name", "quality_rating", "status")}
        # Calidad RED o un número que no está CONNECTED limitan los envíos.
        if body.get("quality_rating") == "RED" or body.get("status") not in (None, "CONNECTED"):
            return _result("degraded", "El número tiene calidad baja o no está conectado.", started, data)
        return _result("operational", None, started, data)

    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    if response.status_code in (400, 401) and error.get("code") == 190:
        return _result("down", "El token de WhatsApp venció o es inválido (error 190).", started)
    if response.status_code in (400, 404) and error.get("code") in (100, 33):
        return _result("down", "WHATSAPP_PHONE_NUMBER_ID no existe o el token no tiene acceso.", started)
    if response.status_code == 429 or error.get("code") in (4, 17, 80007):
        return _result("degraded", "La Graph API limitó las consultas (rate limit).", started)
    return _result("degraded", f"La Graph API respondió {response.status_code}.", started)


# --- Orquestación ------------------------------------------------------------


def _combine(platform: dict, credentials: dict | None) -> dict:
    return {
        "status": worst(platform["status"], credentials["status"] if credentials else None),
        "platform": platform,
        "credentials": credentials,
    }


async def check_integrations(probe: bool = True, force: bool = False) -> dict:
    """Salud de OpenAI y WhatsApp. `probe=False` salta el sondeo con llaves
    (solo páginas de estado públicas). El resultado se cachea CACHE_TTL_SECONDS
    para que el panel pueda refrescar seguido sin pegarle a los proveedores."""
    cache_key = "probe" if probe else "platform"
    cached = _cache.get(cache_key)
    if cached and not force and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        tasks = [openai_platform_status(client), whatsapp_platform_status(client)]
        if probe:
            tasks += [openai_credentials_check(client), whatsapp_credentials_check(client)]
        results = await asyncio.gather(*tasks)

    openai = _combine(results[0], results[2] if probe else None)
    whatsapp = _combine(results[1], results[3] if probe else None)
    payload = {
        "checked_at": datetime.now(timezone.utc),
        "status": worst(openai["status"], whatsapp["status"]),
        "openai": openai,
        "whatsapp": whatsapp,
    }
    _cache[cache_key] = (time.monotonic(), payload)
    return payload
