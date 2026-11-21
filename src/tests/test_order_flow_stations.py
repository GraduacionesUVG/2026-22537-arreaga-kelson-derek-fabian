"""Fase 0/3: numeración, eventos, estaciones y ventana de pago."""

import asyncio
from datetime import datetime, timedelta, timezone

from src.core.security import hash_pin


def _order(client, restaurant_id, items):
    return client.post(
        "/api/orders/",
        json={"student_id": "20231", "restaurant_id": restaurant_id, "items": items},
    )


def test_full_flow_with_numbers_events_and_modifiers(client, seed, db):
    kds_headers, kds = seed["login_kds"]("gh-uvg")
    r = _order(client, seed["gh_id"], [{"product_name": "Hamburguesa", "quantity": 2, "modifiers": ["Doble queso", "sin tomate", "Extra salsa"]}])
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["request_number"] == 1001
    assert order["offered_kds_id"] == kds["kds_user_id"]
    assert order["total_amount"] == 60.0  # (25 + 5) * 2
    mods = order["items"][0]["modifiers"]
    assert mods[2] == {"name": "Extra salsa", "price": 0.0, "custom": True}

    # La otra estación no ve ni puede aceptar la solicitud
    spazio_headers, _ = seed["login_kds"]("gh-spazio")
    assert client.get("/api/orders/active", headers=spazio_headers).json() == []
    r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=spazio_headers)
    assert r.status_code == 403

    active = client.get("/api/orders/active", headers=kds_headers).json()
    assert [o["id"] for o in active] == [order["id"]]

    r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=kds_headers)
    assert r.status_code == 200, r.text
    accepted = r.json()
    assert accepted["assigned_kds_id"] == kds["kds_user_id"]
    assert accepted["payment_expires_at"] is not None

    r = client.post("/webhooks/bac-payment", json={"order_id": order["id"], "status": "success"})
    paid = r.json()["order"]
    assert paid["order_number"] == 2001

    for status_ in ("in_preparation", "ready_for_pickup"):
        r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": status_}, headers=kds_headers)
        assert r.status_code == 200, r.text

    assert client.post(f"/api/orders/{order['id']}/validate-otp", json={"otp_code": "xxxx"}, headers=kds_headers).status_code == 400
    r = client.post(f"/api/orders/{order['id']}/validate-otp", json={"otp_code": paid["otp_code"]}, headers=kds_headers)
    assert r.status_code == 200

    events = asyncio.new_event_loop().run_until_complete(db.order_events.find({"order_id": order["id"]}).sort("seq", 1).to_list(None))
    names = [e["event"] for e in events]
    assert names == [
        "order_created", "order_accepted", "payment_link_sent", "order_paid",
        "order_in_preparation", "order_ready", "otp_failed", "order_delivered",
    ]
    assert events[0]["message"].startswith("Solicitud #1001: 2x Hamburguesa por Q60.00")
    assert events[1]["message"] == "Solicitud #1001 aceptada por KDS gh-uvg"
    assert "Orden #2001" in events[3]["message"]


def test_status_requires_token(client, seed):
    r = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}])
    assert client.put(f"/api/orders/{r.json()['id']}/status", json={"pipeline_status": "accepted"}).status_code in (401, 403)


def test_active_orders_scoped_by_restaurant(client, seed):
    _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}])
    _order(client, seed["gi_id"], [{"product_name": "Papas", "quantity": 1}])
    gi_headers, _ = seed["login_kds"]("gitane1")
    active = client.get("/api/orders/active", headers=gi_headers).json()
    assert len(active) == 1 and active[0]["restaurant_id"] == seed["gi_id"]


def test_mixed_stations_rejected_with_message(client, seed, db):
    asyncio.new_event_loop().run_until_complete(
        db.menu_items.insert_one(
            {"restaurant_id": seed["gh_id"], "code": "CAL-1", "name": "Almuerzo del día - Opción 1",
             "category": "almuerzos del día", "price": 40.0, "available": True, "prep_time_minutes": 10,
             "schedule_type": "always"}
        )
    )
    r = _order(client, seed["gh_id"], [{"product_name": "Hamburguesa", "quantity": 1}, {"product_name": "Almuerzo del día - Opción 1", "quantity": 1}])
    assert r.status_code == 400
    assert "se deben ordenar por separado" in r.json()["detail"]


def test_reoffer_to_next_station_then_reject(client, seed, db):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        db.kds_users.insert_one(
            {"username": "gh-uvg-2", "pin_hash": hash_pin("1234"), "restaurant_name": "Golden Harvest",
             "restaurant_id": seed["gh_id"], "menu_filter": {"mode": "all"}, "priority": 2, "active": True,
             "token_version": 0}
        )
    )
    first_headers, first = seed["login_kds"]("gh-uvg")
    second_headers, second = seed["login_kds"]("gh-uvg-2")
    order = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).json()
    assert order["offered_kds_id"] == first["kds_user_id"]  # prioridad 1

    r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "rejected"}, headers=first_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["pipeline_status"] == "pending_acceptance"
    assert body["offered_kds_id"] == second["kds_user_id"]
    assert client.get("/api/orders/active", headers=first_headers).json() == []

    r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "rejected"}, headers=second_headers)
    assert r.json()["pipeline_status"] == "rejected"
    assert r.json()["cancel_reason"] == "rejected_by_all_stations"


def test_equal_priority_balances_load(client, seed, db):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(db.kds_users.update_many({"restaurant_id": seed["gh_id"]}, {"$set": {"menu_filter": {"mode": "all"}}}))
    first = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).json()
    second = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).json()
    assert first["offered_kds_id"] != second["offered_kds_id"]


def test_payment_link_expires(client, seed, db):
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    order = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).json()
    client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=kds_headers)

    from src.services.payment_expiry import build_order_service

    later = datetime.now(timezone.utc) + timedelta(minutes=20)
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(build_order_service(db).expire_unpaid_orders(later)) == 1
    doc = loop.run_until_complete(db.orders.find_one({}))
    assert doc["pipeline_status"] == "cancelled"
    assert doc["cancel_reason"] == "payment_link_expired"


def test_payment_deadline_rules():
    from src.services.order_service import compute_payment_deadline

    now = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
    asap = compute_payment_deadline(now, {"requested_ready_time": None, "items": []})
    assert asap - now == timedelta(minutes=10)
    far = compute_payment_deadline(now, {"requested_ready_time": now + timedelta(hours=3), "items": [{"prep_time_minutes": 10}]})
    assert far - now == timedelta(minutes=15)
    near = compute_payment_deadline(now, {"requested_ready_time": now + timedelta(minutes=12), "items": [{"prep_time_minutes": 10}]})
    assert near - now == timedelta(minutes=5)
