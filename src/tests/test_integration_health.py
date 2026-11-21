"""Health de OpenAI y WhatsApp: GET /health/integrations y src/services/integration_health.py.

Todo con httpx.MockTransport: no se usan llaves reales ni se sale a internet.
"""

import asyncio

import httpx
import pytest

from src.configuration import settings
from src.services import integration_health as ih


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _json(body, status=200):
    return httpx.Response(status, json=body)


OPENAI_OK = {
    "components": [
        {"name": "Chat Completions", "status": "operational"},
        {"name": "Audio", "status": "operational"},
        {"name": "Images", "status": "major_outage"},  # no es de los que usamos
    ],
    "incidents": [],
}
META_OK = [
    {"id": "otro", "services": [{"name": "Cloud API", "status": "Outage"}]},
    {"id": "whatsapp-business-api", "services": [{"name": "Cloud API", "status": "No known issues"}]},
]


async def _platform(fn, body, status=200):
    async with _client(lambda request: _json(body, status)) as client:
        return await fn(client)


def test_openai_plataforma_ignora_componentes_que_no_usamos():
    assert _run(_platform(ih.openai_platform_status, OPENAI_OK))["status"] == "operational"


def test_openai_plataforma_incidente():
    body = {
        "components": [
            {"name": "Chat Completions", "status": "partial_outage"},
            {"name": "Audio", "status": "operational"},
        ],
        "incidents": [{"name": "Elevated errors"}],
    }
    result = _run(_platform(ih.openai_platform_status, body))
    assert result["status"] == "degraded" and "Elevated errors" in result["detail"]
    body["components"][0]["status"] = "major_outage"
    assert _run(_platform(ih.openai_platform_status, body))["status"] == "down"


@pytest.mark.parametrize("body,status", [({"components": []}, 200), ({}, 500), ("no es json", 200)])
def test_openai_plataforma_formato_raro_es_unknown(body, status):
    async def scenario():
        async with _client(lambda request: httpx.Response(status, json=body) if status != 200 or isinstance(body, dict) else httpx.Response(200, text=body)) as client:
            return await ih.openai_platform_status(client)

    assert _run(scenario())["status"] == "unknown"


def test_meta_plataforma_usa_solo_whatsapp_cloud_api():
    assert _run(_platform(ih.whatsapp_platform_status, META_OK))["status"] == "operational"
    META_OK[1]["services"][0]["status"] = "Service disruption"
    result = _run(_platform(ih.whatsapp_platform_status, META_OK))
    META_OK[1]["services"][0]["status"] = "No known issues"
    assert result["status"] == "degraded" and "Cloud API" in result["detail"]


def test_meta_plataforma_si_cambia_el_formato_es_unknown():
    assert _run(_platform(ih.whatsapp_platform_status, {"orgs": []}))["status"] == "unknown"
    assert _run(_platform(ih.whatsapp_platform_status, [{"id": "otro"}]))["status"] == "unknown"


async def _creds(fn, response):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return response

    async with _client(handler) as client:
        result = await fn(client)
    return result, seen


@pytest.mark.parametrize(
    "status,expected",
    [(200, "operational"), (401, "down"), (404, "down"), (429, "degraded"), (503, "degraded")],
)
def test_openai_credenciales(monkeypatch, status, expected):
    monkeypatch.setattr(settings.openai, "api_key", "sk-test")
    monkeypatch.setattr(settings.openai, "model", "gpt-test")
    result, seen = _run(_creds(ih.openai_credentials_check, _json({}, status)))
    assert result["status"] == expected
    assert seen["auth"] == "Bearer sk-test" and seen["url"].endswith("/v1/models/gpt-test")


def test_openai_sin_llave_es_down_y_no_sale_a_internet(monkeypatch):
    monkeypatch.setattr(settings.openai, "api_key", "")
    result, seen = _run(_creds(ih.openai_credentials_check, _json({})))
    assert result["status"] == "down" and seen == {}


def _whatsapp(monkeypatch):
    monkeypatch.setattr(settings.whatsapp, "token", "tok")
    monkeypatch.setattr(settings.whatsapp, "phone_number_id", "123")


def test_whatsapp_credenciales_ok_trae_calidad(monkeypatch):
    _whatsapp(monkeypatch)
    body = {"display_phone_number": "+502 5555 0000", "quality_rating": "GREEN", "status": "CONNECTED"}
    result, seen = _run(_creds(ih.whatsapp_credentials_check, _json(body)))
    assert result["status"] == "operational" and result["data"]["quality_rating"] == "GREEN"
    assert seen["auth"] == "Bearer tok" and "/123?" in seen["url"]


def test_whatsapp_calidad_roja_es_degraded(monkeypatch):
    _whatsapp(monkeypatch)
    result, _ = _run(_creds(ih.whatsapp_credentials_check, _json({"quality_rating": "RED", "status": "CONNECTED"})))
    assert result["status"] == "degraded"


def test_whatsapp_token_vencido_es_down(monkeypatch):
    _whatsapp(monkeypatch)
    error = {"error": {"code": 190, "message": "Error validating access token: Session has expired"}}
    result, _ = _run(_creds(ih.whatsapp_credentials_check, _json(error, 401)))
    assert result["status"] == "down" and "venció" in result["detail"]


def test_whatsapp_sin_credenciales_es_down(monkeypatch):
    monkeypatch.setattr(settings.whatsapp, "token", "")
    result, seen = _run(_creds(ih.whatsapp_credentials_check, _json({})))
    assert result["status"] == "down" and seen == {}


def test_worst_ordena_por_gravedad():
    assert ih.worst("operational", "unknown") == "unknown"
    assert ih.worst("operational", "degraded", "unknown") == "degraded"
    assert ih.worst("degraded", "down") == "down"
    assert ih.worst(None) == "unknown"


# --- Ruta --------------------------------------------------------------------


@pytest.fixture()
def fake_checks(monkeypatch):
    ih._cache.clear()

    async def ok_platform(client):
        return ih._result("operational")

    async def bad_creds(client):
        return ih._result("down", "token vencido")

    monkeypatch.setattr(ih, "openai_platform_status", ok_platform)
    monkeypatch.setattr(ih, "whatsapp_platform_status", ok_platform)
    monkeypatch.setattr(ih, "openai_credentials_check", ok_platform)
    monkeypatch.setattr(ih, "whatsapp_credentials_check", bad_creds)
    yield
    ih._cache.clear()


def test_ruta_solo_superadmin(client, seed, fake_checks):
    assert client.get("/health/integrations").status_code in (401, 403)
    assert client.get("/health/integrations", headers=seed["login_admin"]("admin@gh.com")).status_code == 403
    r = client.get("/health/integrations", headers=seed["login_admin"]("ti@uvg.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "down" and body["openai"]["status"] == "operational"
    assert body["whatsapp"]["credentials"]["detail"] == "token vencido"


def test_ruta_sin_sondeo_no_usa_llaves(client, seed, fake_checks):
    r = client.get("/health/integrations?probe=false", headers=seed["login_admin"]("ti@uvg.com"))
    body = r.json()
    assert body["status"] == "operational" and body["whatsapp"]["credentials"] is None


def test_resultado_se_cachea_y_refresh_lo_ignora(client, seed, fake_checks, monkeypatch):
    ti = seed["login_admin"]("ti@uvg.com")
    calls = []

    async def counting(client_):
        calls.append(1)
        return ih._result("operational")

    monkeypatch.setattr(ih, "openai_platform_status", counting)
    client.get("/health/integrations?probe=false", headers=ti)
    client.get("/health/integrations?probe=false", headers=ti)
    assert len(calls) == 1
    client.get("/health/integrations?probe=false&refresh=true", headers=ti)
    assert len(calls) == 2
