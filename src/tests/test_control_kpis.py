"""Fases 4 y 5: eventos, línea de tiempo, consola en vivo y KPIs."""


def _complete_order(client, seed, product="Hamburguesa", student="S1", kds="gh-uvg"):
    headers, _ = seed["login_kds"](kds)
    order = client.post("/api/orders/", json={"student_id": student, "restaurant_id": seed["gh_id"], "items": [{"product_name": product, "quantity": 1}]}).json()
    client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=headers)
    paid = client.post("/webhooks/bac-payment", json={"order_id": order["id"], "status": "success"}).json()["order"]
    for status_ in ("in_preparation", "ready_for_pickup"):
        client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": status_}, headers=headers)
    client.post(f"/api/orders/{order['id']}/validate-otp", json={"otp_code": paid["otp_code"]}, headers=headers)
    return order


def test_events_timeline_lookup(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    order = _complete_order(client, seed)
    day = client.get("/api/admin/events", headers=gh).json()
    assert day["live"] is True
    assert day["events"][0]["message"].startswith("Solicitud #1001")
    assert [e["seq"] for e in day["events"]] == sorted(e["seq"] for e in day["events"])

    timeline = client.get(f"/api/admin/orders/{order['id']}/timeline", headers=gh).json()
    assert timeline["order"]["order_number"] == 2001
    assert timeline["events"][-1]["event"] == "order_delivered"

    assert client.get("/api/admin/orders/lookup?order_number=2001", headers=gh).json()["id"] == order["id"]
    gi = seed["login_admin"]("admin@gitane.com")
    assert client.get("/api/admin/orders/lookup?order_number=2001", headers=gi).status_code == 404
    assert client.get(f"/api/admin/orders/{order['id']}/timeline", headers=gi).status_code == 404
    assert client.get("/api/admin/events", headers=gi).json()["events"] == []
    assert len(client.get("/api/admin/orders", headers=gh).json()) == 1
    assert client.get("/api/admin/events?date=2020-01-01", headers=gh).json()["live"] is False


def test_admin_websocket_backfill_and_live(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    token = gh["Authorization"].split()[1]
    client.post("/api/orders/", json={"student_id": "S1", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Café", "quantity": 1}]})
    with client.websocket_connect(f"/ws/admin?token={token}") as ws:
        backfill = ws.receive_json()
        assert backfill["type"] == "backfill" and len(backfill["events"]) == 1
        client.post("/api/orders/", json={"student_id": "S2", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Café", "quantity": 1}]})
        live = ws.receive_json()
        assert live["type"] == "event" and live["event"]["request_number"] == 1002

    ti = seed["login_admin"]("ti@uvg.com")
    token = ti["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/admin?token={token}&restaurant_id=all") as ws:
        assert len(ws.receive_json()["events"]) == 2


def test_kpis(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    _complete_order(client, seed, "Hamburguesa", "S1")
    _complete_order(client, seed, "Hamburguesa", "S1")
    _complete_order(client, seed, "Café", "S2")
    # Aceptada y abandonada
    headers, kds = seed["login_kds"]("gh-uvg")
    order = client.post("/api/orders/", json={"student_id": "S3", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Café", "quantity": 1}]}).json()
    client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=headers)
    client.post("/webhooks/bac-payment", json={"order_id": order["id"], "status": "failed"})

    k = client.get("/api/metrics/kpis", headers=gh).json()
    assert k["ventas"]["total"] == 60.0 and k["ventas"]["por_dia"][0]["pedidos"] == 3
    assert k["plato_mas_pedido"]["total"] == {"nombre": "Hamburguesa", "cantidad": 2}
    assert k["tiempo_preparacion_min"]["muestras"] == 3
    assert k["hora_pico"]["hora_pico_global"]["pedidos"] == 4
    assert k["abandono"] == {"tasa": 0.25, "abandonados": 1, "aceptados": 4, "rechazados_por_cocina": 0}
    assert k["recompra"]["usuarios_1_pedido"] == 1 and k["recompra"]["usuarios_2_o_mas"] == 1

    ks = client.get(f"/api/metrics/kpis?scope=kds&kds_user_id={kds['kds_user_id']}", headers=gh).json()
    assert ks["resumen"]["pedidos_exitosos"] == 3
    filtered = client.get("/api/metrics/kpis?from=2020-01-01&to=2020-01-02", headers=gh).json()
    assert filtered["hora_pico"]["hora_pico_filtrado"] is None and filtered["hora_pico"]["hora_pico_global"] is not None

    assert client.get("/api/metrics/kpis?scope=global", headers=gh).status_code == 403
    ti = seed["login_admin"]("ti@uvg.com")
    g = client.get("/api/metrics/kpis?scope=global", headers=ti).json()
    assert "plato_mas_pedido" not in g and g["resumen"]["solicitudes"] == 4
    assert client.get("/api/metrics/dashboard", headers=gh).status_code == 200
