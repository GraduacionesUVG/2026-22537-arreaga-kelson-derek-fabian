"""Turnos de estaciones KDS: inicio, cierre, numeración por día y resúmenes por turno y por día."""

import asyncio
from datetime import timedelta

from src.core.security import hash_pin
from src.core.timeutils import local_date_str, local_day_bounds_utc, local_today


def _run(db_coro):
    return asyncio.new_event_loop().run_until_complete(db_coro)


def _order(client, restaurant_id, product, quantity=1):
    r = client.post(
        "/api/orders/",
        json={"student_id": "20231", "restaurant_id": restaurant_id, "items": [{"product_name": product, "quantity": quantity}]},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _second_station(db, seed):
    _run(
        db.kds_users.insert_one(
            {"username": "gh-uvg-2", "pin_hash": hash_pin("1234"), "restaurant_name": "Golden Harvest",
             "restaurant_id": seed["gh_id"], "menu_filter": {"mode": "all"}, "priority": 2, "active": True,
             "token_version": 0}
        )
    )


def test_start_and_end_shift_toggle_station(client, seed):
    headers, _ = seed["login_kds"]("gh-uvg")
    current = client.get("/api/kds/shift/current", headers=headers).json()
    assert current["shift"] is None and current["kpis"] is None

    r = client.post("/api/kds/shift/start", headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["station_active"] is True and body["previous_shift_closed"] is None
    shift = body["shift"]
    assert shift["shift_no"] == 1 and shift["status"] == "open" and shift["local_date"] == local_date_str()
    assert shift["label"].startswith("Turno 1 · ") and shift["started_at"].endswith("-06:00")

    r = client.post("/api/kds/shift/start", headers=headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "shift_already_open"

    current = client.get("/api/kds/shift/current", headers=headers).json()
    assert current["shift"]["id"] == shift["id"] and current["kpis"]["total_tickets"] == 0

    r = client.post("/api/kds/shift/end", headers=headers)
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["shift"]["status"] == "closed" and summary["shift"]["hora_fin"] is not None
    assert summary["tickets"] == [] and summary["kpis"]["pct_rechazadas"] == 0.0
    assert client.get("/api/kds/me", headers=headers).json()["active"] is False

    assert client.post("/api/kds/shift/end", headers=headers).json()["detail"]["code"] == "no_open_shift"
    second = client.post("/api/kds/shift/start", headers=headers).json()["shift"]
    assert second["shift_no"] == 2

    shifts = client.get("/api/kds/shifts", headers=headers).json()
    assert [s["shift"]["shift_no"] for s in shifts] == [1, 2]


def test_pause_and_resume_require_open_shift(client, seed):
    headers, _ = seed["login_kds"]("gh-uvg")
    for active in (False, True):
        r = client.put("/api/kds/me/active", json={"active": active}, headers=headers)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_open_shift"
    client.post("/api/kds/shift/start", headers=headers)
    assert client.put("/api/kds/me/active", json={"active": False}, headers=headers).status_code == 200
    assert client.put("/api/kds/me/active", json={"active": True}, headers=headers).json()["active"] is True


def test_pauses_recorded_in_shift(client, seed, db):
    headers, kds = seed["login_kds"]("gh-uvg")
    client.post("/api/kds/shift/start", headers=headers)
    shift = client.get("/api/kds/shift/current", headers=headers).json()["shift"]
    assert shift["pausas"] == {"cantidad": 0, "tiempo_total_min": 0.0, "en_pausa": False, "detalle": []}

    client.put("/api/kds/me/active", json={"active": False}, headers=headers)
    client.put("/api/kds/me/active", json={"active": False}, headers=headers)  # ya en pausa: no cuenta otra
    pausas = client.get("/api/kds/shift/current", headers=headers).json()["shift"]["pausas"]
    assert pausas["cantidad"] == 1 and pausas["en_pausa"] is True and pausas["detalle"][0]["hora_fin"] is None
    client.put("/api/kds/me/active", json={"active": True}, headers=headers)

    # Pausa desde el panel de administración también se registra
    gh = seed["login_admin"]("admin@gh.com")
    client.put(f"/api/admin/kds-users/{kds['kds_user_id']}/active", json={"active": False}, headers=gh)

    # Simula 10 min en cada pausa
    shift_doc = _run(db.kds_shifts.find_one({"status": "open"}))
    pauses = shift_doc["pauses"]
    for pause in pauses:
        pause["started_at"] = pause["started_at"] - timedelta(minutes=10)
    pauses[0]["ended_at"] = pauses[0]["started_at"] + timedelta(minutes=10)
    _run(db.kds_shifts.update_one({"_id": shift_doc["_id"]}, {"$set": {"pauses": pauses, "started_at": shift_doc["started_at"] - timedelta(minutes=30)}}))

    # Finalizar con la estación en pausa cierra también la pausa abierta
    summary = client.post("/api/kds/shift/end", headers=headers).json()
    pausas = summary["shift"]["pausas"]
    assert pausas["cantidad"] == 2 and pausas["en_pausa"] is False
    assert 19.9 <= pausas["tiempo_total_min"] <= 20.5
    assert pausas["detalle"][1]["started_by"]["type"] == "admin" and pausas["detalle"][1]["ended_by"]["type"] == "kds"
    assert all(p["hora_fin"] for p in pausas["detalle"])
    assert 9.5 <= summary["shift"]["tiempo_activo_min"] <= 10.5


def test_shift_summary_kpis_and_rejections(client, seed, db):
    _second_station(db, seed)
    headers, kds = seed["login_kds"]("gh-uvg")
    second_headers, second = seed["login_kds"]("gh-uvg-2")
    client.post("/api/kds/shift/start", headers=headers)
    client.post("/api/kds/shift/start", headers=second_headers)

    # 1) aceptada y pagada (Q50)
    paid = _order(client, seed["gh_id"], "Hamburguesa", 2)
    assert client.put(f"/api/orders/{paid['id']}/status", json={"pipeline_status": "accepted"}, headers=headers).status_code == 200
    client.post("/webhooks/bac-payment", json={"order_id": paid["id"], "status": "success"})
    # 2) rechazada por carga: pasa a la otra estación, que la acepta
    load = _order(client, seed["gh_id"], "Café")
    r = client.post(f"/api/orders/{load['id']}/reject", json={"reason": "kitchen_load"}, headers=headers)
    assert r.json()["offered_kds_id"] == second["kds_user_id"]
    client.put(f"/api/orders/{load['id']}/status", json={"pipeline_status": "accepted"}, headers=second_headers)
    # 3) pendiente: no se puede cerrar el turno
    stock = _order(client, seed["gh_id"], "Café")
    r = client.post("/api/kds/shift/end", headers=headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "pending_requests" and r.json()["detail"]["por_aceptar"] == 1
    assert client.get("/api/kds/shift/current", headers=headers).json()["pendientes"]["por_aceptar"] == 1
    # ... rechazada por falta de stock
    r = client.post(
        f"/api/orders/{stock['id']}/reject",
        json={"reason": "out_of_stock", "out_of_stock_item_ids": [stock["items"][0]["menu_item_id"]]},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = client.post("/api/kds/shift/end", headers=headers)
    assert r.status_code == 200, r.text
    summary = r.json()
    kpis = summary["kpis"]
    assert kpis["total_tickets"] == 3
    assert kpis["aceptadas"] == 1 and kpis["ordenes_pagadas"] == 1 and kpis["total_dinero"] == 50.0
    assert kpis["rechazadas"] == 2 and kpis["rechazadas_carga"] == 1 and kpis["rechazadas_stock"] == 1
    assert kpis["pct_rechazadas"] == 66.7 and kpis["pct_rechazadas_carga"] == 33.3
    assert summary["pendientes"] == {"por_aceptar": 0, "en_curso": 1}
    results = {t["request_number"]: t["resultado"] for t in summary["tickets"]}
    assert results == {
        paid["request_number"]: "en_curso",
        load["request_number"]: "rechazada_carga",
        stock["request_number"]: "rechazada_stock",
    }

    # La otra estación ve el ticket que recibió por carga como aceptado (esperando pago)
    other = client.get("/api/kds/shift/current", headers=second_headers).json()
    assert other["kpis"]["total_tickets"] == 1 and other["kpis"]["esperando_pago"] == 1

    # Resumen guardado: se puede volver a consultar; otra estación no lo ve
    shift_id = summary["shift"]["id"]
    assert client.get(f"/api/kds/shifts/{shift_id}", headers=headers).json()["kpis"]["total_tickets"] == 3
    assert client.get(f"/api/kds/shifts/{shift_id}", headers=second_headers).status_code == 404

    # Admin: resumen del día (restaurante) y detalle del turno
    gh = seed["login_admin"]("admin@gh.com")
    day = client.get(f"/api/admin/shifts?date={local_date_str()}", headers=gh)
    assert day.status_code == 200, day.text
    day = day.json()
    assert day["kpis"]["total_tickets"] == 3 and day["kpis"]["sin_turno"] == 0
    assert day["kpis"]["rechazadas"] == 1 and day["kpis"]["rechazadas_stock"] == 1
    assert day["kpis"]["aceptadas"] == 2 and day["kpis"]["total_dinero"] == 50.0
    assert len(day["turnos"]) == 2
    by_number = {t["request_number"]: t for t in day["tickets"]}
    assert by_number[load["request_number"]]["kds_username"] == "gh-uvg-2"
    assert by_number[load["request_number"]]["turno"]["shift_no"] == 1

    station_day = client.get(f"/api/admin/shifts?kds_user_id={kds['kds_user_id']}", headers=gh).json()
    assert station_day["kpis"]["rechazadas_carga"] == 1 and len(station_day["turnos"]) == 1

    assert client.get(f"/api/admin/shifts/{shift_id}", headers=gh).json()["kpis"]["total_dinero"] == 50.0
    gi = seed["login_admin"]("admin@gitane.com")
    assert client.get(f"/api/admin/shifts/{shift_id}", headers=gi).status_code == 404
    ti = seed["login_admin"]("ti@uvg.com")
    assert client.get("/api/admin/shifts", headers=ti).status_code == 400
    assert client.get(f"/api/admin/shifts?restaurant_id={seed['gh_id']}", headers=ti).json()["kpis"]["total_tickets"] == 3


def test_orders_outside_shift_are_flagged_in_day_summary(client, seed):
    _order(client, seed["gh_id"], "Café")
    gh = seed["login_admin"]("admin@gh.com")
    day = client.get("/api/admin/shifts", headers=gh).json()
    assert day["kpis"]["total_tickets"] == 1 and day["kpis"]["sin_turno"] == 1
    assert day["tickets"][0]["turno"] is None and day["tickets"][0]["resultado"] == "pendiente"


def test_stale_shift_closed_at_restaurant_closing_time(client, seed, db):
    headers, kds = seed["login_kds"]("gh-uvg")
    # "Ayer" en hora de Guatemala (no en UTC): entre las 18:00 y las 23:59 locales el día UTC ya cambió y
    # el turno no se consideraría de un día anterior.
    yesterday = local_today() - timedelta(days=1)
    started = local_day_bounds_utc(yesterday)[0] + timedelta(hours=8)  # 08:00 en Guatemala
    _run(
        db.kds_shifts.insert_one(
            {"restaurant_id": seed["gh_id"], "kds_user_id": kds["kds_user_id"], "kds_username": "gh-uvg",
             "local_date": local_date_str(started), "shift_no": 1, "status": "open", "started_at": started,
             "ended_at": None, "end_adjusted": False, "started_by": {"type": "kds", "id": kds["kds_user_id"], "username": "gh-uvg"},
             "ended_by": None, "created_at": started, "updated_at": started}
        )
    )
    current = client.get("/api/kds/shift/current", headers=headers).json()
    assert current["shift"]["stale"] is True
    assert client.put("/api/kds/me/active", json={"active": True}, headers=headers).status_code == 409

    r = client.post("/api/kds/shift/start", headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    previous = body["previous_shift_closed"]
    assert previous["status"] == "closed" and previous["end_adjusted"] is True
    assert previous["hora_fin"] == "23:59"  # ALWAYS_OPEN cierra a las 23:59
    assert previous["ended_by"]["type"] == "system"
    assert body["shift"]["shift_no"] == 1 and body["shift"]["local_date"] == local_date_str()


def test_shift_status_broadcast(client, seed):
    headers, _ = seed["login_kds"]("gh-uvg")
    token = headers["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/kds/{seed['gh_id']}?token={token}") as ws:
        client.post("/api/kds/shift/start", headers=headers)
        messages = [ws.receive_json(), ws.receive_json()]
        assert {m["type"] for m in messages} == {"station_status", "shift_status"}
        shift_msg = next(m for m in messages if m["type"] == "shift_status")
        assert shift_msg["status"] == "open" and shift_msg["shift"]["shift_no"] == 1
        client.post("/api/kds/shift/end", headers=headers)
        messages = [ws.receive_json(), ws.receive_json()]
        assert next(m for m in messages if m["type"] == "shift_status")["status"] == "closed"
        assert next(m for m in messages if m["type"] == "station_status")["active"] is False
