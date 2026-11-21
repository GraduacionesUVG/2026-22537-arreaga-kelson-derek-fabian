"""CORS de los frontends: orígenes permitidos por CORS_ORIGINS."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.cors import DEV_ORIGINS, parse_origins, setup_cors

ADMIN = "https://qorder-admin.onrender.com"
KDS = "https://qorder-kds.onrender.com"
EVIL = "https://evil.example.com"


def test_parse_origins_limpia_separadores_y_barras():
    raw = f" {ADMIN}/ ,{KDS}\n{ADMIN} ,, "
    assert parse_origins(raw) == [ADMIN, KDS]  # sin barra final, sin duplicados ni vacíos


def test_parse_origins_vacio():
    assert parse_origins("") == []
    assert parse_origins(None) == []


def test_dev_origins_cubren_los_cuatro_frontends():
    assert "http://localhost:5173" in DEV_ORIGINS and "http://localhost:5176" in DEV_ORIGINS


def _app(origins: list[str], regex: str = "") -> tuple[FastAPI, TestClient]:
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    enabled = setup_cors(app, origins, regex)
    assert enabled == bool(origins or regex)
    return app, TestClient(app)


def test_sin_origenes_no_agrega_cors():
    _, client = _app([])
    response = client.get("/ping", headers={"Origin": ADMIN})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_origen_permitido_recibe_cabeceras():
    _, client = _app([ADMIN])
    response = client.get("/ping", headers={"Origin": ADMIN})
    assert response.headers["access-control-allow-origin"] == ADMIN
    assert "access-control-allow-credentials" not in response.headers  # el token va en Authorization
    assert "Content-Disposition" in response.headers["access-control-expose-headers"]


def test_origen_no_permitido_no_recibe_cabeceras():
    _, client = _app([ADMIN])
    response = client.get("/ping", headers={"Origin": EVIL})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_con_authorization_permitido():
    _, client = _app([ADMIN])
    response = client.options(
        "/ping",
        headers={
            "Origin": ADMIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ADMIN
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "content-type" in allowed
    assert "POST" in response.headers["access-control-allow-methods"]


def test_preflight_de_origen_no_permitido_falla():
    _, client = _app([ADMIN])
    response = client.options(
        "/ping", headers={"Origin": EVIL, "Access-Control-Request-Method": "GET"}
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_regex_permite_previews():
    _, client = _app([], regex=r"https://qorder-[a-z]+-pr-\d+\.onrender\.com")
    ok = client.get("/ping", headers={"Origin": "https://qorder-admin-pr-12.onrender.com"})
    assert ok.headers["access-control-allow-origin"] == "https://qorder-admin-pr-12.onrender.com"
    bad = client.get("/ping", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in bad.headers


# --- La app real (main.py), con CORS_ORIGINS tomado de tests/conftest.py ------------------------


def test_app_real_responde_cors_en_health(client):
    response = client.get("/health/", headers={"Origin": "https://qorder-admin.onrender.com"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://qorder-admin.onrender.com"


def test_app_real_preflight_de_ruta_protegida(client):
    response = client.options(
        "/api/admin/menu/categories",
        headers={
            "Origin": "https://qorder-admin.onrender.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://qorder-admin.onrender.com"


def test_app_real_rechaza_origen_desconocido(client):
    response = client.get("/health/", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in response.headers
