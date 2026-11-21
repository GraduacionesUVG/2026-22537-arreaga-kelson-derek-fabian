"""Regla de oro: solo se pide con el restaurante abierto y para hoy.

Cubre el estado `status` de restaurantes/menú, el 409 de los endpoints de
pedidos y la pausa de la estación KDS con su propio token (con aviso en vivo
a todas sus pantallas).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from src.core.timeutils import GUATEMALA_TZ, utc_now
from src.services.restaurant_availability import (
    RestaurantUnavailableError,
    ensure_can_order,
    restaurant_availability,
)

WEEK = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
NINE_TO_FIVE = {d: {"closed": False, "open": "09:00", "close": "17:00"} for d in WEEK}
CLOSED_ALL_WEEK = {d: {"closed": True, "open": None, "close": None} for d in WEEK}
RESTAURANT = {"id": "r1", "name": "Gitane", "active": True, "opening_hours": NINE_TO_FIVE}


def _gt(hour, minute=0, day=13):
    """Martes 13/10/2026 a esa hora de Guatemala, en UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=GUATEMALA_TZ).astimezone(timezone.utc)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _set_hours(db, restaurant_id, hours):
    _run(db.restaurants.update_one({"_id": ObjectId(restaurant_id)}, {"$set": {"opening_hours": hours}}))


def _order_body(restaurant_id, product="Papas", **extra):
    return {"student_id": "1", "restaurant_id": restaurant_id, "items": [{"product_name": product, "quantity": 1}], **extra}


# --- Cálculo puro --------------------------------------------------------------------


def test_open_inside_hours():
    a = restaurant_availability(RESTAURANT, [], _gt(12))
    assert a["status"] == "open" and a["is_open_now"] is True and a["closed_reason"] is None
    assert a["closes_at"] == "5:00 pm" and a["opens_at"] == "9:00 am"


def test_closed_before_opening_today():
    a = restaurant_availability(RESTAURANT, [], _gt(7))
    assert a["status"] == "closed" and a["closed_reason"] == "kitchen_closed"
    assert a["next_opening"] == "hoy a las 9:00 am"
    assert "Abre hoy a las 9:00 am" in a["status_message"]


def test_closed_after_closing_opens_tomorrow():
    a = restaurant_availability(RESTAURANT, [], _gt(18))
    assert a["closed_reason"] == "kitchen_closed" and a["next_opening"] == "mañana a las 9:00 am"


def test_last_order_cutoff_30_minutes_before_close(monkeypatch):
    from src.configuration import settings

    monkeypatch.setattr(settings.payment, "last_order_minutes_before_close", 30)
    open_ = restaurant_availability(RESTAURANT, [], _gt(16, 29))
    assert open_["status"] == "open" and open_["last_order_at"] == "4:30 pm"
    assert "último pedido a las 4:30 pm" in open_["status_message"]

    closing = restaurant_availability(RESTAURANT, [], _gt(16, 30))
    assert closing["status"] == "closed" and closing["closed_reason"] == "kitchen_closing"
    assert closing["next_opening"] == "mañana a las 9:00 am"
    assert "último pedido es a las 4:30 pm" in closing["status_message"]
    try:
        ensure_can_order(RESTAURANT, [], _gt(16, 45))
        raise AssertionError("debía rechazar")
    except RestaurantUnavailableError as exc:
        assert exc.code == "kitchen_closing" and exc.to_detail()["last_order_at"] == "4:30 pm"


def test_orders_rejected_in_last_30_minutes(client, seed, db, monkeypatch):
    from src.configuration import settings

    monkeypatch.setattr(settings.payment, "last_order_minutes_before_close", 30)
    now_local = utc_now().astimezone(GUATEMALA_TZ)
    if now_local.hour == 0 and now_local.minute < 30:
        return  # no se puede armar un cierre "dentro de 20 min" sin cruzar la medianoche
    close = (now_local + timedelta(minutes=20)).strftime("%H:%M")
    if close < now_local.strftime("%H:%M"):
        return  # cerca de la medianoche: el cierre caería al día siguiente
    hours = {d: {"closed": False, "open": "00:00", "close": close} for d in WEEK}
    _set_hours(db, seed["gi_id"], hours)
    r = client.post("/api/orders/", json=_order_body(seed["gi_id"]))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "kitchen_closing"
    gitane = client.get(f"/api/restaurants/{seed['gi_id']}").json()
    assert gitane["status"] == "closed" and gitane["closed_reason"] == "kitchen_closing"


def test_all_stations_paused_is_out_of_service():
    stations = [{"active": False}, {"active": False}]
    a = restaurant_availability(RESTAURANT, stations, _gt(12))
    assert a["status"] == "closed" and a["closed_reason"] == "kitchen_out_of_service"
    assert restaurant_availability(RESTAURANT, [{"active": False}, {"active": True}], _gt(12))["status"] == "open"


def test_requested_time_must_be_today_and_before_close():
    tomorrow_noon = _gt(12, day=14)
    try:
        ensure_can_order(RESTAURANT, [], _gt(12), tomorrow_noon)
        raise AssertionError("debía rechazar")
    except RestaurantUnavailableError as exc:
        assert exc.code == "outside_opening_hours"
    try:
        ensure_can_order(RESTAURANT, [], _gt(12), _gt(18))
        raise AssertionError("debía rechazar")
    except RestaurantUnavailableError as exc:
        assert exc.code == "outside_opening_hours" and "5:00 pm" in exc.message
    ensure_can_order(RESTAURANT, [], _gt(12), _gt(16))  # hoy, antes del cierre: OK


# --- API -------------------------------------------------------------------------------


def test_restaurants_list_includes_status(client, seed, db):
    _set_hours(db, seed["gi_id"], CLOSED_ALL_WEEK)
    restaurants = {r["name"]: r for r in client.get("/api/restaurants/", params={"active_only": True}).json()}
    assert restaurants["Golden Harvest"]["status"] == "open"
    gitane = restaurants["Gitane"]
    assert gitane["status"] == "closed" and gitane["closed_reason"] == "kitchen_closed"
    assert gitane["status_message"].startswith("Gitane está cerrado")
    assert client.get(f"/api/restaurants/{seed['gi_id']}").json()["status"] == "closed"


def test_menu_items_include_restaurant_status(client, seed, db):
    _set_hours(db, seed["gi_id"], CLOSED_ALL_WEEK)
    items = client.get("/api/menus/", params={"restaurant_id": seed["gi_id"]}).json()
    assert items and all(i["restaurant_status"] == "closed" for i in items)
    assert items[0]["restaurant_closed_reason"] == "kitchen_closed"
    gh_items = client.get("/api/menus/", params={"restaurant_id": seed["gh_id"]}).json()
    assert all(i["restaurant_status"] == "open" for i in gh_items)


def test_orders_rejected_when_closed(client, seed, db):
    _set_hours(db, seed["gi_id"], CLOSED_ALL_WEEK)
    for path in ("/api/orders/estimate", "/api/orders/"):
        body = _order_body(seed["gi_id"])
        if path.endswith("estimate"):
            body.pop("student_id")
        r = client.post(path, json=body)
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "kitchen_closed"
        assert r.json()["detail"]["status"] == "closed"
    assert _run(db.orders.count_documents({})) == 0


def test_orders_for_another_day_rejected(client, seed):
    tomorrow = (utc_now().astimezone(GUATEMALA_TZ) + timedelta(days=1)).replace(hour=12, minute=0, tzinfo=None)
    r = client.post("/api/orders/", json=_order_body(seed["gi_id"], requested_ready_time=tomorrow.isoformat()))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "outside_opening_hours"
    estimate = {"restaurant_id": seed["gi_id"], "items": [{"product_name": "Papas", "quantity": 1}],
                "desired_ready_time": tomorrow.isoformat()}
    r = client.post("/api/orders/estimate", json=estimate)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "outside_opening_hours"


def test_order_rejected_when_all_stations_paused(client, seed, db):
    _run(db.kds_users.update_many({"restaurant_id": seed["gh_id"]}, {"$set": {"active": False}}))
    r = client.post("/api/orders/", json=_order_body(seed["gh_id"], product="Café"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "kitchen_out_of_service"
    gh = next(x for x in client.get("/api/restaurants/").json() if x["id"] == seed["gh_id"])
    assert gh["status"] == "closed" and gh["closed_reason"] == "kitchen_out_of_service"


# --- Pausa de la estación con token de KDS ---------------------------------------------


def test_kds_pause_with_own_token_syncs_all_screens(client, seed):
    headers, _ = seed["login_kds"]("gh-uvg")
    client.post("/api/kds/shift/start", headers=headers)  # reanudar requiere un turno abierto
    token = headers["Authorization"].split()[1]
    url = f"/ws/kds/{seed['gh_id']}?token={token}"
    with client.websocket_connect(url) as tablet_a, client.websocket_connect(url) as tablet_b:
        r = client.put("/api/kds/me/active", json={"active": False}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False
        for ws in (tablet_a, tablet_b):
            message = ws.receive_json()
            assert message["type"] == "station_status" and message["active"] is False
        assert client.get("/api/kds/me", headers=headers).json()["active"] is False

        client.put("/api/kds/me/active", json={"active": True}, headers=headers)
        assert tablet_a.receive_json()["active"] is True
        assert tablet_b.receive_json()["active"] is True


def test_admin_pause_also_notifies_station(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    headers, kds = seed["login_kds"]("gh-uvg")
    token = headers["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/kds/{seed['gh_id']}?token={token}") as ws:
        r = client.put(f"/api/admin/kds-users/{kds['kds_user_id']}/active", json={"active": False}, headers=gh)
        assert r.status_code == 200, r.text
        message = ws.receive_json()
        assert message["type"] == "station_status" and message["active"] is False
        assert message["kds_user_id"] == kds["kds_user_id"]
