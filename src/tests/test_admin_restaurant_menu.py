"""Fase 1: alcance, horario, ubicación y CRUD de menú."""


from src.core.timeutils import local_today
from src.models.restaurant import WEEKDAYS


def _week(**overrides):
    days = {d: {"closed": False, "open": "08:00", "close": "20:00"} for d in WEEKDAYS}
    days["sunday"] = {"closed": True}
    days.update(overrides)
    return days


def test_scope_rules(client, seed):
    gi = seed["login_admin"]("admin@gitane.com")
    assert client.get(f"/api/admin/restaurant?restaurant_id={seed['gh_id']}", headers=gi).status_code == 403
    assert client.get("/api/admin/restaurant", headers=gi).json()["name"] == "Gitane"
    ti = seed["login_admin"]("ti@uvg.com")
    assert client.get("/api/admin/restaurant", headers=ti).status_code == 400
    assert client.get(f"/api/admin/restaurant?restaurant_id={seed['gh_id']}", headers=ti).json()["name"] == "Golden Harvest"
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    assert client.get("/api/admin/restaurant", headers=kds_headers).status_code == 403


def test_legacy_admin_role_is_normalized(client, seed):
    r = client.post("/auth/admin/login", json={"email": "admin@gh.com", "password": "secret123"})
    assert r.json()["role"] == "restaurant_admin"
    assert r.json()["restaurant_id"] == seed["gh_id"]


def test_opening_hours_and_location(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.put("/api/admin/restaurant/opening-hours", json=_week(saturday={"closed": False, "open": "09:00", "close": "13:00"}), headers=gh)
    assert r.status_code == 200, r.text
    hours = r.json()["opening_hours"]
    assert hours["saturday"] == {"closed": False, "open": "09:00", "close": "13:00"}
    assert hours["sunday"]["closed"] is True

    bad = _week()
    bad.pop("monday")
    assert client.put("/api/admin/restaurant/opening-hours", json=bad, headers=gh).status_code == 422
    assert client.put("/api/admin/restaurant/opening-hours", json=_week(monday={"open": "20:00", "close": "08:00"}), headers=gh).status_code == 422

    r = client.put("/api/admin/restaurant/location", json={"location": "Frente a la biblioteca"}, headers=gh)
    assert r.json()["location"] == "Frente a la biblioteca"
    public = client.get(f"/api/restaurants/{seed['gh_id']}").json()
    assert public["location"] == "Frente a la biblioteca"


def test_menu_crud_and_public_listing(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    item = {
        "name": "Wrap",
        "category": "comida",
        "price": 30,
        "prep_time_minutes": 6,
        "modifiers": [{"name": "Extra pollo", "price": 8, "group": "Extras"}],
    }
    r = client.post("/api/admin/menu", json=item, headers=gh)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["code"] == "WRAP-01"
    assert created["modifiers"][0]["price"] == 8

    assert client.post("/api/admin/menu", json=item, headers=gh).status_code == 422  # mismo nombre, mismo día

    today = local_today()
    weekday = WEEKDAYS[today.weekday()]
    other_day = WEEKDAYS[(today.weekday() + 1) % 7]
    for day in (weekday, other_day):
        r = client.post("/api/admin/menu", json={"name": "Almuerzo semanal", "category": "almuerzos", "price": 35, "schedule_type": "weekly", "day_of_week": day}, headers=gh)
        assert r.status_code == 201, r.text
    assert client.post("/api/admin/menu", json={"name": "Sin día", "category": "x", "price": 1, "schedule_type": "weekly"}, headers=gh).status_code == 422

    r = client.put(f"/api/admin/menu/{created['id']}", json={"price": 32.5, "available": False}, headers=gh)
    assert r.json()["price"] == 32.5 and r.json()["available"] is False

    public_names = [i["name"] for i in client.get(f"/api/menus/?restaurant_id={seed['gh_id']}").json()]
    assert public_names.count("Almuerzo semanal") == 1
    assert "Wrap" in public_names

    assert client.delete(f"/api/admin/menu/{created['id']}", headers=gh).status_code == 204
    assert client.get(f"/api/admin/menu/{created['id']}", headers=gh).status_code == 404
    public_names = [i["name"] for i in client.get(f"/api/menus/?restaurant_id={seed['gh_id']}").json()]
    assert "Wrap" not in public_names

    gi = seed["login_admin"]("admin@gitane.com")
    assert client.get(f"/api/admin/menu/{created['id']}", headers=gi).status_code == 404
    cats = client.get("/api/admin/menu/categories", headers=gh).json()
    assert "almuerzos" in cats


def test_cutoff_and_stock(client, seed, db):
    gh = seed["login_admin"]("admin@gh.com")
    client.post("/api/admin/menu", json={"name": "Pastel", "category": "comida", "price": 12, "daily_stock": 1}, headers=gh)
    client.post("/api/admin/menu", json={"name": "Desayuno", "category": "comida", "price": 20, "order_cutoff": "00:00"}, headers=gh)
    menu = {i["name"]: i for i in client.get(f"/api/menus/?restaurant_id={seed['gh_id']}").json()}
    assert "Desayuno" not in menu or menu["Desayuno"]["order_cutoff"] == "00:00"
    assert menu["Pastel"]["remaining_stock"] == 1

    order = lambda: client.post("/api/orders/", json={"student_id": "1", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Pastel", "quantity": 1}]})
    assert order().status_code == 201
    r = order()
    assert r.status_code == 400
