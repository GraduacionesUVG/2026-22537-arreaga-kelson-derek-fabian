"""
Cliente HTTP hacia el backend de QOrder (GP-Agent-Backend).

agent/ corre como un proceso aparte del backend (webhook.py es su propio
servicio), así que las tools no importan
repositorios ni tocan Mongo directamente -- hablan con el backend por su API
HTTP. BACKEND_API_URL sale de agent/.env (ver agent/.env.example).

Todas las rutas que usa el agente exigen el header X-Internal-Api-Key:
`backend_request` lo agrega siempre.
"""

import os
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv()

BACKEND_API_URL = os.getenv("BACKEND_API_URL", "http://localhost:8000")
# Obligatorio: el mismo valor que INTERNAL_API_KEY del backend. Sin él, el
# backend responde 503 (o 401 si no coincide).
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")


class BackendError(Exception):
    """Error controlado (4xx/5xx) del backend -- `detail` ya viene en español,
    listo para que la tool se lo devuelva al modelo tal cual."""

    def __init__(self, detail: str, status_code: int):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


def backend_request(method: str, path: str, **kwargs) -> dict | list:
    """GET/POST/PUT contra el backend. Lanza BackendError en 4xx/5xx en vez
    de dejar que httpx tire su propia excepción -- cada tool decide cómo
    traducir eso a un mensaje para el modelo (ver agent/tools.py)."""
    # 30s en vez de un default más corto: el backend está en el plan free de
    # Render, que apaga el servicio tras inactividad -- el primer request
    # después de eso ("cold start") puede tardar bastante en responder.
    kwargs["headers"] = {**_internal_headers(), **(kwargs.get("headers") or {})}
    with httpx.Client(base_url=BACKEND_API_URL, timeout=30.0) as client:
        response = client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise BackendError(detail, response.status_code)
        if response.status_code == 204:
            return {}
        return response.json()


def check_whatsapp_registration(wa_id: str) -> dict:
    """Lo que `agent/` debe llamar ANTES de contestar cualquier mensaje. `wa_id` es tal como lo manda Meta en el
    webhook (`message["from"]`), sin el `+` -- acá se antepone antes de
    llamar al backend, que sí lo espera en formato E.164."""
    return backend_request(
        "POST",
        "/auth/whatsapp/check",
        json={"whatsapp_number": "+" + wa_id}
    )


def _internal_headers() -> dict:
    return {"X-Internal-Api-Key": INTERNAL_API_KEY} if INTERNAL_API_KEY else {}


def report_conversation_metrics(payload: dict) -> bool:
    """Manda los tiempos de un pedido al backend. Best-effort: nunca lanza, y usa un timeout corto
    (no el de 30 s de backend_request) para no dejar al estudiante esperando su siguiente mensaje."""
    try:
        response = httpx.post(
            f"{BACKEND_API_URL}/api/internal/conversation-metrics",
            json=payload,
            headers=_internal_headers(),
            timeout=8.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        print(f"[metrics] No se pudieron guardar los tiempos del pedido {payload.get('order_id')}: {exc}")
        return False


# --- Historial y memoria por estudiante (ver agent/memory.py) -----------------
# /api/users/* exige INTERNAL_API_KEY, como el resto (backend_request lo agrega).


def get_order_history(student_id: str) -> dict:
    return backend_request("GET", f"/api/users/{quote(student_id, safe='')}/order-history")


def get_user_memory(student_id: str) -> dict:
    return backend_request("GET", f"/api/users/{quote(student_id, safe='')}/memory")


def save_user_memory(student_id: str, sections: dict) -> dict:
    """PUT de solo las secciones enviadas (preferences / summaries / session)."""
    return backend_request(
        "PUT", f"/api/users/{quote(student_id, safe='')}/memory", json=sections
    )


def delete_user_memory(student_id: str) -> None:
    backend_request("DELETE", f"/api/users/{quote(student_id, safe='')}/memory")
