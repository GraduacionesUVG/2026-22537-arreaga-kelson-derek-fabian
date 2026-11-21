"""Solicitudes que el restaurante no atiende: se rechazan solas a los 20 minutos."""

import asyncio
from datetime import datetime, timedelta, timezone

from src.configuration import settings
from src.services import notification_templates
from src.services.payment_expiry import build_order_service


def _order(client, restaurant_id, product="Café"):
    r = client.post(
        "/api/orders/",
        json={"student_id": "20231", "restaurant_id": restaurant_id, "items": [{"product_name": product, "quantity": 1}]},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _expire(db, minutes_later: float) -> int:
    later = datetime.now(timezone.utc) + timedelta(minutes=minutes_later)
    return _run(build_order_service(db).expire_unanswered_requests(later))


def _doc(db, order_id):
    from bson import ObjectId

    return _run(db.orders.find_one({"_id": ObjectId(order_id)}))


def test_solicitud_sin_respuesta_se_rechaza_a_los_20_minutos(client, seed, db):
    order_id = _order(client, seed["gh_id"])
    assert _expire(db, 25) == 1

    doc = _doc(db, order_id)
    assert doc["pipeline_status"] == "rejected"
    assert doc["cancel_reason"] == "no_response"
    assert doc["reject_reason"] == "no_response"
    assert doc["out_of_stock_items"] == []
    assert doc["cancelled_at"] is not None


def test_antes_de_los_20_minutos_no_se_toca(client, seed, db):
    order_id = _order(client, seed["gh_id"])
    assert _expire(db, 19) == 0
    assert _doc(db, order_id)["pipeline_status"] == "pending_acceptance"


def test_se_puede_cambiar_el_plazo(client, seed, db, monkeypatch):
    monkeypatch.setattr(settings.payment, "request_timeout_minutes", 5)
    order_id = _order(client, seed["gh_id"])
    assert _expire(db, 6) == 1
    assert _doc(db, order_id)["pipeline_status"] == "rejected"


def test_con_plazo_cero_nunca_expira(client, seed, db, monkeypatch):
    monkeypatch.setattr(settings.payment, "request_timeout_minutes", 0)
    order_id = _order(client, seed["gh_id"])
    assert _expire(db, 24 * 60) == 0
    assert _doc(db, order_id)["pipeline_status"] == "pending_acceptance"


def test_lo_aceptado_no_se_cierra_solo_por_este_mecanismo(client, seed, db):
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    order_id = _order(client, seed["gh_id"])
    r = client.put(f"/api/orders/{order_id}/status", json={"pipeline_status": "accepted"}, headers=kds_headers)
    assert r.status_code == 200, r.text

    assert _expire(db, 60) == 0  # aceptado y sin pagar: lo cierra el vencimiento del link, no este plazo
    assert _doc(db, order_id)["pipeline_status"] == "accepted"


def test_lo_pagado_nunca_se_cierra_solo(client, seed, db):
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    order_id = _order(client, seed["gh_id"])
    client.put(f"/api/orders/{order_id}/status", json={"pipeline_status": "accepted"}, headers=kds_headers)
    client.post("/webhooks/bac-payment", json={"order_id": order_id, "status": "success"})
    assert _doc(db, order_id)["payment_status"] == "paid"

    assert _expire(db, 24 * 60) == 0
    assert _doc(db, order_id)["pipeline_status"] != "rejected"


def test_la_solicitud_rechazada_sale_de_la_cola_del_kds(client, seed, db):
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    order_id = _order(client, seed["gh_id"])
    assert [o["id"] for o in client.get("/api/orders/active", headers=kds_headers).json()] == [order_id]

    _expire(db, 25)
    assert client.get("/api/orders/active", headers=kds_headers).json() == []


def test_queda_registrada_en_el_log_del_panel(client, seed, db):
    _order(client, seed["gh_id"])
    _expire(db, 25)
    event = _run(db.order_events.find_one({"event": "order_rejected"}))
    assert event is not None
    assert "el restaurante no respondió a tiempo" in event["message"]


def test_el_estudiante_recibe_el_aviso_de_que_no_respondio(client, seed, db):
    order_id = _order(client, seed["gh_id"], "Café")
    _expire(db, 25)
    text = notification_templates.order_rejected_message(_doc(db, order_id) | {"id": order_id})
    assert "el restaurante no respondió a tiempo" in text
    assert "Pedido: Café" in text
    assert "intentarlo de nuevo" in text


def test_es_idempotente(client, seed, db):
    _order(client, seed["gh_id"])
    assert _expire(db, 25) == 1
    assert _expire(db, 26) == 0
