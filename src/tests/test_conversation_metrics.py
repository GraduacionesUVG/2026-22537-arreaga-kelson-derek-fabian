"""POST /api/internal/conversation-metrics y GET /api/metrics/conversation-times (objetivo: pedido < 45 s)."""

import pytest

from src.configuration import settings

KEY = "test-internal-key"
HEADERS = {"X-Internal-Api-Key": KEY}


@pytest.fixture(autouse=True)
def internal_key(monkeypatch):
    monkeypatch.setattr(settings.auth, "internal_api_key", KEY)


def _body(order_id="o1", start="2026-10-20T15:00:00Z", end="2026-10-20T15:00:38Z", handle_ms=(2000, 3500)):
    return {
        "order_id": order_id,
        "first_message_at": start,
        "order_created_at": end,
        "turns": [
            {"input_type": "audio" if i == 0 else "text", "audio_ms": 1500 if i == 0 else 0, "llm_ms": 900,
             "tools_ms": 300, "handle_ms": ms, "tools": ["estimar_pedido"]}
            for i, ms in enumerate(handle_ms)
        ],
    }


def test_exige_api_key(db):
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)  # sin el header que el fixture `client` manda por default
    assert client.post("/api/internal/conversation-metrics", json=_body()).status_code == 401


def test_falla_cerrado_sin_key_configurada(client, db, monkeypatch):
    monkeypatch.setattr(settings.auth, "internal_api_key", "")
    assert client.post("/api/internal/conversation-metrics", json=_body(), headers=HEADERS).status_code == 503


def test_guarda_tiempos_calculados_en_el_backend(client, db):
    r = client.post("/api/internal/conversation-metrics", json=_body(), headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json() == {"order_id": "o1", "duration_s": 38.0, "bot_processing_s": 5.5, "turns": 2}
    import asyncio

    doc = asyncio.new_event_loop().run_until_complete(db.conversation_metrics.find_one({"order_id": "o1"}))
    assert doc["input_types"] == ["audio", "text"]
    assert doc["local_date"] == "2026-10-20"
    assert "student_id" not in doc


def test_reintento_no_duplica(client, db):
    for _ in range(2):
        assert client.post("/api/internal/conversation-metrics", json=_body(), headers=HEADERS).status_code == 200
    import asyncio

    assert asyncio.new_event_loop().run_until_complete(db.conversation_metrics.count_documents({})) == 1


def test_rechaza_sin_turnos(client, db):
    body = _body()
    body["turns"] = []
    assert client.post("/api/internal/conversation-metrics", json=body, headers=HEADERS).status_code == 422


def test_resumen_solo_superadmin(client, seed):
    client.post("/api/internal/conversation-metrics", json=_body("a", end="2026-10-20T15:00:30Z"), headers=HEADERS)
    client.post("/api/internal/conversation-metrics", json=_body("b", end="2026-10-20T15:01:10Z"), headers=HEADERS)

    assert client.get("/api/metrics/conversation-times").status_code in (401, 403)
    assert client.get("/api/metrics/conversation-times", headers=seed["login_admin"]("admin@gh.com")).status_code == 403

    r = client.get("/api/metrics/conversation-times", headers=seed["login_admin"]("ti@uvg.com"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["pedidos"] == 2
    assert data["objetivo_segundos"] == 45.0
    assert data["tiempo_total_s"]["pct_bajo_45s"] == 0.5  # 30 s sí, 70 s no
    assert data["procesamiento_bot_s"]["pct_bajo_45s"] == 1.0


def test_resumen_filtra_por_fecha(client, seed):
    client.post("/api/internal/conversation-metrics", json=_body("a"), headers=HEADERS)
    r = client.get("/api/metrics/conversation-times?from=2026-10-21", headers=seed["login_admin"]("ti@uvg.com"))
    assert r.json()["pedidos"] == 0
