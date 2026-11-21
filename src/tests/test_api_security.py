"""Rutas de servicio (solo agent/), documentación apagada en PRODUCTION y headers de seguridad."""

import pytest
from fastapi.testclient import TestClient

from main import app
from src.configuration import settings
from tests.conftest import INTERNAL_KEY

AGENT_ROUTES = [
    ("GET", "/api/restaurants/", None),
    ("GET", "/api/restaurants/665f00000000000000000000", None),
    ("GET", "/api/menus/", None),
    ("POST", "/api/orders/estimate", {}),
    ("POST", "/api/orders/", {}),
    ("GET", "/api/orders/665f00000000000000000000/status", None),
    ("POST", "/auth/whatsapp/check", {"whatsapp_number": "+50255555555"}),
    ("POST", "/webhooks/whatsapp", {"message": "hola"}),
    ("GET", "/api/users/12345/memory", None),
]


@pytest.mark.parametrize("method,path,body", AGENT_ROUTES)
@pytest.mark.parametrize("headers", [{}, {"X-Internal-Api-Key": "otra"}])
def test_rutas_del_agente_exigen_api_key(db, method, path, body, headers):
    response = TestClient(app).request(method, path, json=body, headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", AGENT_ROUTES)
def test_rutas_del_agente_fallan_cerrado_sin_key_configurada(db, monkeypatch, method, path, body):
    monkeypatch.setattr(settings.auth, "internal_api_key", "")
    response = TestClient(app).request(method, path, json=body, headers={"X-Internal-Api-Key": ""})
    assert response.status_code == 503


def test_con_api_key_pasa(client, seed):
    response = client.get("/api/restaurants/")
    assert response.status_code == 200
    assert {r["name"] for r in response.json()} >= {"Golden Harvest", "Gitane"}


def test_headers_de_seguridad_en_json(client, db):
    response = client.get("/health/")
    h = response.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cache-control"] == "no-store"
    assert h["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert "strict-transport-security" not in h  # solo en PRODUCTION


def test_headers_de_seguridad_tambien_en_errores(db):
    response = TestClient(app).get("/api/menus/")
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"


def test_hsts_en_production():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from src.core.security_headers import SecurityHeadersMiddleware

    mini = Starlette(routes=[Route("/", lambda r: JSONResponse({}))])
    mini.add_middleware(SecurityHeadersMiddleware, hsts=True)
    assert TestClient(mini).get("/").headers["strict-transport-security"].startswith("max-age=31536000")
