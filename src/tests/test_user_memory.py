"""/api/users/{student_id}/... -- historial de pedidos y memoria del agente de WhatsApp."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.configuration import settings
from src.models.user_memory import normalize_user_memory

KEY = "test-internal-key"
HEADERS = {"X-Internal-Api-Key": KEY}
STUDENT = "5024-carne-demo"


@pytest.fixture(autouse=True)
def internal_key(monkeypatch):
    monkeypatch.setattr(settings.auth, "internal_api_key", KEY)


def _insert(db, collection, docs):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(db[collection].insert_many(docs))
    loop.close()


def _order(restaurant_id, items, *, paid=True, status="delivered", days_ago=0, hour_utc=19, student=STUDENT):
    created = datetime(2026, 9, 20, hour_utc, 0, tzinfo=timezone.utc) - timedelta(days=days_ago)
    return {
        "student_id": student,
        "restaurant_id": restaurant_id,
        "items": [{"product_name": name, "quantity": qty, "unit_price": price} for name, qty, price in items],
        "total_amount": sum(qty * price for _, qty, price in items),
        "pipeline_status": status,
        "payment_status": "paid" if paid else "pending",
        "requested_ready_time": None,
        "created_at": created,
    }


# --- Seguridad ----------------------------------------------------------------


def test_sin_header_da_401(db):
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)  # sin el header que el fixture `client` manda por default
    assert client.get(f"/api/users/{STUDENT}/memory").status_code == 401
    assert client.get(f"/api/users/{STUDENT}/memory", headers={"X-Internal-Api-Key": "otra"}).status_code == 401
    assert client.get(f"/api/users/{STUDENT}/order-history").status_code == 401


def test_sin_internal_api_key_configurado_falla_cerrado(client, db, monkeypatch):
    monkeypatch.setattr(settings.auth, "internal_api_key", "")
    # Aunque el cliente mande cualquier header, sin llave configurada no pasa nadie.
    assert client.get(f"/api/users/{STUDENT}/memory", headers=HEADERS).status_code == 503


# --- Historial de pedidos -----------------------------------------------------


def test_historial_cuenta_solo_pedidos_pagados_y_no_cancelados(client, seed, db):
    gh, gi = seed["gh_id"], seed["gi_id"]
    _insert(
        db,
        "orders",
        [
            _order(gh, [("Hamburguesa", 1, 25.0), ("Café", 2, 10.0)], days_ago=3),
            _order(gh, [("Hamburguesa", 2, 25.0)], days_ago=2),
            _order(gi, [("Papas", 1, 15.0)], days_ago=1, status="in_preparation"),
            # No cuentan:
            _order(gh, [("Hamburguesa", 9, 25.0)], paid=False, status="accepted"),
            _order(gh, [("Café", 9, 10.0)], status="cancelled"),
            _order(gh, [("Café", 9, 10.0)], status="rejected"),
            _order(gh, [("Café", 9, 10.0)], student="otro-estudiante"),
        ],
    )

    r = client.get(f"/api/users/{STUDENT}/order-history", headers=HEADERS)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_orders"] == 3
    assert body["most_ordered"] == [
        {"product": "Hamburguesa", "count": 3},
        {"product": "Café", "count": 2},
        {"product": "Papas", "count": 1},
    ]
    assert body["frequent_restaurants"][0] == {"restaurant": "Golden Harvest", "count": 2}
    assert body["tried_products"] == ["Café", "Hamburguesa", "Papas"]
    # El más nuevo primero.
    assert body["recent_orders"][0]["restaurant"] == "Gitane"
    assert body["recent_orders"][0]["items"] == ["1x Papas"]
    # 19:00 UTC = 13:00 en Guatemala.
    assert body["usual_hour"] == "13:00"
    assert body["average_spend"] == pytest.approx((45 + 50 + 15) / 3, abs=0.01)


def test_historial_vacio(client, db):
    body = client.get(f"/api/users/{STUDENT}/order-history", headers=HEADERS).json()

    assert body["total_orders"] == 0
    assert body["most_ordered"] == [] and body["usual_hour"] is None and body["average_spend"] is None


# --- Memoria ------------------------------------------------------------------


def test_memoria_inexistente_devuelve_forma_vacia(client, db):
    body = client.get(f"/api/users/{STUDENT}/memory", headers=HEADERS).json()

    assert body["student_id"] == STUDENT
    assert body["schema_version"] == 1
    assert body["preferences"] == {"likes": [], "dislikes": [], "restrictions": [], "usual_budget": None}
    assert body["summaries"] == []
    assert body["session"]["turns"] == []


def test_put_crea_y_solo_reemplaza_las_secciones_enviadas(client, db):
    session = {
        "started_at": "2026-09-28T18:00:00Z",
        "last_message_at": "2026-09-28T18:05:00Z",
        "turns": [[{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¡Hola!"}]],
    }
    r = client.put(
        f"/api/users/{STUDENT}/memory",
        json={
            "preferences": {"likes": ["Hamburguesa"], "dislikes": ["papas"], "usual_budget": 20},
            "summaries": [{"date": "2026-09-27T21:20:00Z", "text": "Preguntó por smoothies."}],
            "session": session,
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    # Solo preferences: summaries y session quedan como estaban.
    r = client.put(
        f"/api/users/{STUDENT}/memory", json={"preferences": {"likes": ["Café"]}}, headers=HEADERS
    )
    body = r.json()
    assert body["preferences"]["likes"] == ["Café"]
    assert body["summaries"][0]["text"] == "Preguntó por smoothies."
    assert body["session"]["turns"] == session["turns"]
    assert body["created_at"] is not None and body["updated_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"preferences": {"likes": [f"item {n}" for n in range(11)]}},
        {"preferences": {"dislikes": ["x" * 61]}},
        {"preferences": {"usual_budget": -5}},
        {"summaries": [{"date": "2026-09-27T21:20:00Z", "text": "x"}] * 4},
        {"summaries": [{"date": "2026-09-27T21:20:00Z", "text": "x" * 301}]},
        {"session": {"turns": [[{"role": "user", "content": "hola"}]] * 9}},
    ],
    ids=["mas_de_10_items", "item_largo", "presupuesto_negativo", "mas_de_3_resumenes", "resumen_largo", "mas_de_8_turnos"],
)
def test_put_respeta_los_limites(client, db, payload):
    r = client.put(f"/api/users/{STUDENT}/memory", json=payload, headers=HEADERS)

    assert r.status_code == 422


def test_delete_borra_la_memoria(client, db):
    client.put(f"/api/users/{STUDENT}/memory", json={"preferences": {"likes": ["Café"]}}, headers=HEADERS)

    assert client.delete(f"/api/users/{STUDENT}/memory", headers=HEADERS).status_code == 204
    assert client.get(f"/api/users/{STUDENT}/memory", headers=HEADERS).json()["preferences"]["likes"] == []


def test_documento_viejo_sin_secciones_se_normaliza():
    """Documentos guardados antes de agregar una sección se leen con defaults."""
    doc = {"student_id": STUDENT, "preferences": {"likes": ["Café"]}}

    memory = normalize_user_memory(doc, STUDENT)

    assert memory["schema_version"] == 1
    assert memory["preferences"] == {"likes": ["Café"], "dislikes": [], "restrictions": [], "usual_budget": None}
    assert memory["summaries"] == []
    assert memory["session"] == {"started_at": None, "last_message_at": None, "turns": []}
