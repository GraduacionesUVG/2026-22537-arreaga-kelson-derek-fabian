"""Fase 3: administración de estaciones KDS y estado de conexión."""


def test_create_and_manage_station(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.post("/api/admin/kds-users", json={"username": "gh-cafe", "pin": "4321", "priority": 2, "menu_filter": {"mode": "custom", "categories": ["bebidas"]}}, headers=gh)
    assert r.status_code == 201, r.text
    station = r.json()
    assert station["restaurant_name"] == "Golden Harvest" and station["connected"] is False
    assert client.post("/api/admin/kds-users", json={"username": "gh-cafe", "pin": "4321"}, headers=gh).status_code == 400

    old_headers, _ = seed["login_kds"]("gh-cafe", "4321")
    assert client.get("/api/kds/me", headers=old_headers).status_code == 200

    r = client.put(f"/api/admin/kds-users/{station['kds_user_id']}/username", json={"username": "gh-cafeteria"}, headers=gh)
    assert r.json()["username"] == "gh-cafeteria"
    assert client.get("/api/kds/me", headers=old_headers).status_code == 401  # token anterior invalidado

    r = client.put(f"/api/admin/kds-users/{station['kds_user_id']}/pin", json={"pin": "9999"}, headers=gh)
    assert r.status_code == 200
    assert client.post("/auth/kds/login", json={"username": "gh-cafeteria", "pin": "4321"}).status_code == 401
    seed["login_kds"]("gh-cafeteria", "9999")

    r = client.put(f"/api/admin/kds-users/{station['kds_user_id']}/menu-filter", json={"mode": "custom", "item_ids": ["000000000000000000000000"]}, headers=gh)
    assert r.status_code == 400
    items = client.get("/api/admin/menu", headers=gh).json()
    r = client.put(f"/api/admin/kds-users/{station['kds_user_id']}/menu-filter", json={"mode": "custom", "item_ids": [items[0]["id"]], "categories": ["bebidas"]}, headers=gh)
    assert r.json()["menu_filter"]["item_ids"] == [items[0]["id"]]

    assert client.put(f"/api/admin/kds-users/{station['kds_user_id']}/priority", json={"priority": 3}, headers=gh).json()["priority"] == 3
    assert client.put(f"/api/admin/kds-users/{station['kds_user_id']}/active", json={"active": False}, headers=gh).json()["active"] is False

    gi = seed["login_admin"]("admin@gitane.com")
    assert client.put(f"/api/admin/kds-users/{station['kds_user_id']}/priority", json={"priority": 1}, headers=gi).status_code == 404
    assert len(client.get("/api/admin/kds-users", headers=gh).json()) == 3
    ti = seed["login_admin"]("ti@uvg.com")
    assert len(client.get("/api/admin/kds-users?restaurant_id=all", headers=ti).json()) == 4


def test_connection_status_and_ping(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    kds_headers, kds = seed["login_kds"]("gh-uvg")
    token = kds_headers["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/kds/{seed['gh_id']}?token={token}") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
        statuses = {s["username"]: s for s in client.get("/api/admin/kds-users/status", headers=gh).json()}
        assert statuses["gh-uvg"]["connected"] is True
        assert statuses["gh-spazio"]["connected"] is False

        # Pedido nuevo llega a la estación conectada
        client.post("/api/orders/", json={"student_id": "1", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Café", "quantity": 1}]})
        message = ws.receive_json()
        assert message["event"] == "order_created" and message["order"]["request_number"] == 1001
    statuses = {s["username"]: s for s in client.get("/api/admin/kds-users/status", headers=gh).json()}
    assert statuses["gh-uvg"]["connected"] is False and statuses["gh-uvg"]["last_seen"] is not None


def test_kds_wrong_restaurant_rejected(client, seed):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    kds_headers, _ = seed["login_kds"]("gh-uvg")
    token = kds_headers["Authorization"].split()[1]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/kds/{seed['gi_id']}?token={token}") as ws:
            ws.receive_text()
