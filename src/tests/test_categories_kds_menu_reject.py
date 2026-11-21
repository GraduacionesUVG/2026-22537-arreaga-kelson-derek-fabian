"""Categorías desde el panel, menú desde el KDS y rechazo de solicitudes con motivo."""

import asyncio
from datetime import timedelta

from src.core.security import hash_pin
from src.core.timeutils import local_today
from src.services import notification_templates


def _order(client, restaurant_id, items):
    return client.post("/api/orders/", json={"student_id": "20231", "restaurant_id": restaurant_id, "items": items})


def _public_menu(client, restaurant_id):
    return {i["name"]: i for i in client.get(f"/api/menus/?restaurant_id={restaurant_id}").json()}


# --- Categorías ---------------------------------------------------------------------------------


def test_category_create_list_delete(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.post("/api/admin/menu/categories", json={"name": "  Postres "}, headers=gh)
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Postres", "icon": "postres"}  # icono sugerido por el nombre
    assert client.get("/api/admin/menu/categories", headers=gh).json() == ["bebidas", "comida", "Postres"]

    # Duplicada (sin distinguir mayúsculas), también contra las que solo existen en productos
    assert client.post("/api/admin/menu/categories", json={"name": "postres"}, headers=gh).status_code == 409
    assert client.post("/api/admin/menu/categories", json={"name": "Comida"}, headers=gh).status_code == 409
    assert client.post("/api/admin/menu/categories", json={"name": "  "}, headers=gh).status_code == 422

    # Se puede crear un producto con la categoría nueva
    r = client.post("/api/admin/menu", json={"name": "Flan", "category": "Postres", "price": 12}, headers=gh)
    assert r.status_code == 201, r.text
    flan = r.json()

    r = client.delete("/api/admin/menu/categories/postres", headers=gh)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["products_count"] == 1
    assert detail["products"] == [{"id": flan["id"], "code": flan["code"], "name": "Flan"}]
    assert "Aún quedan 1 producto" in detail["message"] and "Flan" in detail["message"]

    assert client.delete(f"/api/admin/menu/{flan['id']}", headers=gh).status_code == 204
    assert client.delete("/api/admin/menu/categories/Postres", headers=gh).status_code == 204
    assert "Postres" not in client.get("/api/admin/menu/categories", headers=gh).json()
    assert client.delete("/api/admin/menu/categories/Postres", headers=gh).status_code == 404


def test_category_in_use_lists_all_products_and_is_scoped(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    gi = seed["login_admin"]("admin@gitane.com")
    client.post("/api/admin/menu", json={"name": "Wrap", "category": "comida", "price": 30}, headers=gh)
    detail = client.delete("/api/admin/menu/categories/comida", headers=gh).json()["detail"]
    assert detail["products_count"] == 2
    assert sorted(p["name"] for p in detail["products"]) == ["Hamburguesa", "Wrap"]

    # La categoría de un restaurante no existe para otro
    client.post("/api/admin/menu/categories", json={"name": "Postres"}, headers=gh)
    assert "Postres" not in client.get("/api/admin/menu/categories", headers=gi).json()
    assert client.delete("/api/admin/menu/categories/Postres", headers=gi).status_code == 404
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    assert client.post("/api/admin/menu/categories", json={"name": "X"}, headers=kds_headers).status_code == 403


# --- Menú desde el KDS ---------------------------------------------------------------------------


def test_kds_menu_list_and_availability(client, seed, db):
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    gh = seed["login_admin"]("admin@gh.com")
    assert client.get("/api/kds/menu").status_code in (401, 403)
    assert client.get("/api/kds/menu", headers=gh).status_code == 403

    menu = client.get("/api/kds/menu", headers=kds_headers).json()
    assert sorted(i["name"] for i in menu) == ["Café", "Hamburguesa"]
    assert all(i["active"] and i["inactive_mode"] is None for i in menu)
    cafe = next(i for i in menu if i["name"] == "Café")
    url = f"/api/kds/menu/{cafe['id']}/availability"

    assert client.patch(url, json={"active": False}, headers=kds_headers).status_code == 422  # falta mode

    # Permanente
    r = client.patch(url, json={"active": False, "mode": "permanent"}, headers=kds_headers)
    assert r.status_code == 200, r.text
    assert (r.json()["active"], r.json()["inactive_mode"], r.json()["available"]) == (False, "permanent", False)
    assert _public_menu(client, seed["gh_id"])["Café"]["available"] is False
    assert _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).status_code == 400

    # Diario
    r = client.patch(url, json={"active": False, "mode": "daily"}, headers=kds_headers)
    assert (r.json()["active"], r.json()["inactive_mode"], r.json()["available"]) == (False, "daily", True)
    assert r.json()["paused_on"] == local_today().isoformat()
    assert _public_menu(client, seed["gh_id"])["Café"]["available"] is False
    assert _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).status_code == 400
    listed = client.get("/api/admin/menu?include_unavailable=false", headers=gh).json()
    assert "Café" not in [i["name"] for i in listed]

    # Al día siguiente vuelve solo (la pausa quedó con fecha de ayer)
    yesterday = (local_today() - timedelta(days=1)).isoformat()
    asyncio.new_event_loop().run_until_complete(db.menu_items.update_one({"name": "Café"}, {"$set": {"paused_on": yesterday}}))
    cafe = next(i for i in client.get("/api/kds/menu", headers=kds_headers).json() if i["name"] == "Café")
    assert cafe["active"] is True and cafe["inactive_mode"] is None
    assert _public_menu(client, seed["gh_id"])["Café"]["available"] is True

    # Activar a mano
    client.patch(url, json={"active": False, "mode": "daily"}, headers=kds_headers)
    r = client.patch(url, json={"active": True}, headers=kds_headers)
    assert r.json()["active"] is True and r.json()["paused_on"] is None
    assert _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).status_code == 201


def test_kds_cannot_touch_other_restaurant_and_admin_reactivates(client, seed):
    gi_headers, _ = seed["login_kds"]("gitane1")
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    gh = seed["login_admin"]("admin@gh.com")
    cafe = next(i for i in client.get("/api/kds/menu", headers=kds_headers).json() if i["name"] == "Café")
    url = f"/api/kds/menu/{cafe['id']}/availability"
    assert client.patch(url, json={"active": False, "mode": "daily"}, headers=gi_headers).status_code == 404
    assert client.patch("/api/kds/menu/no-es-id/availability", json={"active": True}, headers=kds_headers).status_code == 404

    client.patch(url, json={"active": False, "mode": "daily"}, headers=kds_headers)
    r = client.put(f"/api/admin/menu/{cafe['id']}", json={"available": True}, headers=gh)
    assert r.json()["active"] is True and r.json()["paused_on"] is None


# --- Rechazo con motivo -------------------------------------------------------------------------------


def test_reject_kitchen_load(client, seed, db):
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    order = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).json()
    url = f"/api/orders/{order['id']}/reject"

    assert client.post(url, json={"reason": "kitchen_load"}).status_code in (401, 403)
    assert client.post(url, json={"reason": "otra"}, headers=kds_headers).status_code == 422
    assert client.post(url, json={"reason": "kitchen_load", "out_of_stock_item_ids": ["x"]}, headers=kds_headers).status_code == 422
    spazio_headers, _ = seed["login_kds"]("gh-spazio")
    assert client.post(url, json={"reason": "kitchen_load"}, headers=spazio_headers).status_code == 403

    r = client.post(url, json={"reason": "kitchen_load"}, headers=kds_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pipeline_status"] == "rejected"
    assert body["reject_reason"] == "kitchen_load"
    assert body["cancel_reason"] == "rejected_by_all_stations"
    assert body["out_of_stock_items"] == []

    text = notification_templates.order_rejected_message(body)
    assert "mucha carga" in text and text.endswith("Intenta más tarde.")
    # El producto sigue disponible
    assert _public_menu(client, seed["gh_id"])["Café"]["available"] is True

    event = asyncio.new_event_loop().run_until_complete(db.order_events.find_one({"event": "order_rejected"}))
    assert event["message"] == "Solicitud #1001 rechazada por KDS gh-uvg (carga en cocina)"
    assert event["data"]["reject_reason"] == "kitchen_load"

    assert client.post(url, json={"reason": "kitchen_load"}, headers=kds_headers).status_code == 409  # ya rechazada


def test_reject_kitchen_load_reoffers_to_next_station(client, seed, db):
    asyncio.new_event_loop().run_until_complete(
        db.kds_users.insert_one(
            {"username": "gh-uvg-2", "pin_hash": hash_pin("1234"), "restaurant_name": "Golden Harvest",
             "restaurant_id": seed["gh_id"], "menu_filter": {"mode": "all"}, "priority": 2, "active": True,
             "token_version": 0}
        )
    )
    first_headers, _ = seed["login_kds"]("gh-uvg")
    second_headers, second = seed["login_kds"]("gh-uvg-2")
    order = _order(client, seed["gh_id"], [{"product_name": "Café", "quantity": 1}]).json()
    url = f"/api/orders/{order['id']}/reject"

    body = client.post(url, json={"reason": "kitchen_load"}, headers=first_headers).json()
    assert body["pipeline_status"] == "pending_acceptance"
    assert body["offered_kds_id"] == second["kds_user_id"]
    assert body["reject_reason"] is None

    body = client.post(url, json={"reason": "kitchen_load"}, headers=second_headers).json()
    assert body["pipeline_status"] == "rejected" and body["reject_reason"] == "kitchen_load"


def test_reject_out_of_stock_disables_items_for_today(client, seed, db):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        db.kds_users.insert_one(
            {"username": "gh-uvg-2", "pin_hash": hash_pin("1234"), "restaurant_name": "Golden Harvest",
             "restaurant_id": seed["gh_id"], "menu_filter": {"mode": "all"}, "priority": 2, "active": True,
             "token_version": 0}
        )
    )
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    order = _order(
        client, seed["gh_id"],
        [{"product_name": "Café", "quantity": 1}, {"product_name": "Hamburguesa", "quantity": 2}],
    ).json()
    url = f"/api/orders/{order['id']}/reject"
    ids = {i["product_name"]: i["menu_item_id"] for i in order["items"]}

    assert client.post(url, json={"reason": "out_of_stock"}, headers=kds_headers).status_code == 422
    r = client.post(url, json={"reason": "out_of_stock", "out_of_stock_item_ids": ["64b000000000000000000000"]}, headers=kds_headers)
    assert r.status_code == 422 and "no pertenecen al pedido" in r.json()["detail"]

    r = client.post(url, json={"reason": "out_of_stock", "out_of_stock_item_ids": [ids["Hamburguesa"]]}, headers=kds_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # Definitivo aunque haya otra estación disponible
    assert body["pipeline_status"] == "rejected"
    assert body["reject_reason"] == "out_of_stock" and body["cancel_reason"] == "out_of_stock"
    assert body["out_of_stock_items"] == [{"menu_item_id": ids["Hamburguesa"], "name": "Hamburguesa"}]

    text = notification_templates.order_rejected_message(body)
    assert "por hoy ya no hay Hamburguesa." in text

    menu = _public_menu(client, seed["gh_id"])
    assert menu["Hamburguesa"]["available"] is False and menu["Café"]["available"] is True
    assert _order(client, seed["gh_id"], [{"product_name": "Hamburguesa", "quantity": 1}]).status_code == 400

    kds_menu = {i["name"]: i for i in client.get("/api/kds/menu", headers=kds_headers).json()}
    assert kds_menu["Hamburguesa"]["inactive_mode"] == "daily"
    assert kds_menu["Hamburguesa"]["available"] is True  # no es una desactivación permanente

    event = loop.run_until_complete(db.order_events.find_one({"event": "order_rejected"}))
    assert event["message"] == "Solicitud #1001 rechazada por KDS gh-uvg (sin stock: Hamburguesa)"


def test_rejected_message_without_reason_is_unchanged():
    text = notification_templates.order_rejected_message({"items": [{"product_name": "Café", "quantity": 1}]})
    assert text.startswith("Tu pedido no pudo ser aceptado por el restaurante.")
