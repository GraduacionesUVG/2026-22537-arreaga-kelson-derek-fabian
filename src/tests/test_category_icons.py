"""Iconos de categoría: sugerencia, API del panel, renombrado en cascada y snapshot en pedidos."""

import asyncio

import pytest

from src.models.category_icons import CATEGORY_ICON_KEYS, DEFAULT_CATEGORY_ICON, suggest_icon


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _details(client, headers):
    return {c["name"]: c for c in client.get("/api/admin/menu/categories/details", headers=headers).json()}


# --- Sugerencia por nombre ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bebidas", "bebidas"),
        ("Bebidas frías", "bebidas"),
        ("Hamburguesas", "hamburguesas"),
        ("Hot Dogs", "hotdogs"),
        ("HotDogs", "hotdogs"),
        ("Burritos", "burritos"),
        ("Sushi rolls", "sushi"),
        ("Postres", "postres"),
        ("Panadería", "panes"),
        ("Pan dulce", "panes"),
        ("Café caliente", "cafe"),
        ("Sopa de pollo", "sopas"),  # sopas gana a pollo
        ("Ensalada César", "ensaladas"),
        ("Chucherías", "chucherias"),
        ("Papas fritas", "papas"),
        ("Desayunos", "desayuno"),
        ("Almuerzos del día", "almuerzo"),
        ("Comida", DEFAULT_CATEGORY_ICON),
        ("Restaurante", DEFAULT_CATEGORY_ICON),  # "res" solo cuenta como palabra completa
        ("", DEFAULT_CATEGORY_ICON),
        (None, DEFAULT_CATEGORY_ICON),
    ],
)
def test_suggest_icon(name, expected):
    assert suggest_icon(name) == expected


def test_every_suggestion_is_a_known_icon():
    from src.models.category_icons import _KEYWORDS

    assert {key for key, _ in _KEYWORDS} <= set(CATEGORY_ICON_KEYS)
    assert len(set(CATEGORY_ICON_KEYS)) == len(CATEGORY_ICON_KEYS)


# --- API del panel ------------------------------------------------------------------------------


def test_list_icon_keys(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    body = client.get("/api/admin/menu/category-icons", headers=gh).json()
    assert body["icons"] == list(CATEGORY_ICON_KEYS)
    assert body["default"] == DEFAULT_CATEGORY_ICON


def test_create_category_suggests_or_uses_icon(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.post("/api/admin/menu/categories", json={"name": "Postres"}, headers=gh)
    assert r.status_code == 201, r.text
    assert r.json() == {"name": "Postres", "icon": "postres"}

    r = client.post("/api/admin/menu/categories", json={"name": "Antojitos", "icon": "tacos"}, headers=gh)
    assert r.json() == {"name": "Antojitos", "icon": "tacos"}

    r = client.post("/api/admin/menu/categories", json={"name": "Raras", "icon": "no-existe"}, headers=gh)
    assert r.status_code == 422
    assert "Raras" not in client.get("/api/admin/menu/categories", headers=gh).json()


def test_category_details_include_implicit_categories(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    client.post("/api/admin/menu/categories", json={"name": "Postres", "icon": "helados"}, headers=gh)
    details = _details(client, gh)
    # `bebidas` y `comida` solo existen porque un producto las usa: icono sugerido por el nombre
    assert details["bebidas"] == {"name": "bebidas", "icon": "bebidas", "products_count": 1}
    assert details["comida"] == {"name": "comida", "icon": DEFAULT_CATEGORY_ICON, "products_count": 1}
    assert details["Postres"] == {"name": "Postres", "icon": "helados", "products_count": 0}


def test_update_icon_of_implicit_category(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.patch("/api/admin/menu/categories/comida", json={"icon": "hamburguesas"}, headers=gh)
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "comida", "icon": "hamburguesas"}
    assert _details(client, gh)["comida"]["icon"] == "hamburguesas"
    assert client.get("/api/admin/menu/categories", headers=gh).json().count("comida") == 1

    assert client.patch("/api/admin/menu/categories/comida", json={"icon": "xx"}, headers=gh).status_code == 422
    assert client.patch("/api/admin/menu/categories/comida", json={}, headers=gh).status_code == 422
    assert client.patch("/api/admin/menu/categories/nada", json={"icon": "pizza"}, headers=gh).status_code == 404


def test_rename_category_cascades(client, seed, db):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.patch("/api/admin/menu/categories/comida", json={"name": "Platos fuertes"}, headers=gh)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Platos fuertes"

    names = client.get("/api/admin/menu/categories", headers=gh).json()
    assert "comida" not in names and "Platos fuertes" in names
    menu = {i["name"]: i for i in client.get("/api/admin/menu", headers=gh).json()}
    assert menu["Hamburguesa"]["category"] == "Platos fuertes"
    assert menu["Café"]["category"] == "bebidas"  # las demás no se tocan

    # El filtro de la estación KDS que usaba "comida" sigue mostrando esos productos
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    kds_menu = client.get("/api/kds/menu", headers=kds_headers).json()
    assert sorted(i["name"] for i in kds_menu) == ["Café", "Hamburguesa"]
    station = _run(db.kds_users.find_one({"username": "gh-uvg"}))
    assert sorted(station["menu_filter"]["categories"]) == ["Platos fuertes", "bebidas"]
    # y la otra estación no se alteró
    other = _run(db.kds_users.find_one({"username": "gh-spazio"}))
    assert other["menu_filter"]["categories"] == ["almuerzos del día"]


def test_rename_category_conflicts_and_case_only(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.patch("/api/admin/menu/categories/comida", json={"name": "BEBIDAS"}, headers=gh)
    assert r.status_code == 409
    assert client.patch("/api/admin/menu/categories/comida", json={"name": "  "}, headers=gh).status_code == 422

    # Solo cambia mayúsculas: es el mismo nombre, se permite (corrige "psyres" -> "Pizzas")
    client.post("/api/admin/menu/categories", json={"name": "psyres"}, headers=gh)
    r = client.patch("/api/admin/menu/categories/psyres", json={"name": "Pizzas"}, headers=gh)
    assert r.status_code == 200 and r.json() == {"name": "Pizzas", "icon": DEFAULT_CATEGORY_ICON}
    r = client.patch("/api/admin/menu/categories/pizzas", json={"name": "PIZZAS"}, headers=gh)
    assert r.status_code == 200 and r.json()["name"] == "PIZZAS"


def test_rename_is_scoped_to_restaurant(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    gi = seed["login_admin"]("admin@gitane.com")
    assert client.patch("/api/admin/menu/categories/comida", json={"name": "Fuertes"}, headers=gh).status_code == 200
    menu_gi = client.get("/api/admin/menu", headers=gi).json()
    assert [i["category"] for i in menu_gi] == ["comida"]
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    assert client.patch("/api/admin/menu/categories/comida", json={"icon": "pizza"}, headers=kds_headers).status_code == 403


# --- KDS ----------------------------------------------------------------------------------------


def test_kds_menu_and_order_items_carry_category_icon(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    client.patch("/api/admin/menu/categories/bebidas", json={"icon": "jugos"}, headers=gh)

    kds_headers, _ = seed["login_kds"]("gh-uvg")
    menu = {i["name"]: i for i in client.get("/api/kds/menu", headers=kds_headers).json()}
    assert menu["Café"]["category_icon"] == "jugos"  # el guardado gana a la sugerencia
    assert menu["Hamburguesa"]["category_icon"] == DEFAULT_CATEGORY_ICON

    r = client.post(
        "/api/orders/",
        json={
            "student_id": "20231",
            "restaurant_id": seed["gh_id"],
            "items": [{"product_name": "Café", "quantity": 1}, {"product_name": "Hamburguesa", "quantity": 1}],
        },
    )
    assert r.status_code == 201, r.text
    icons = {i["product_name"]: i["category_icon"] for i in r.json()["items"]}
    assert icons == {"Café": "jugos", "Hamburguesa": DEFAULT_CATEGORY_ICON}

    # Cambiar el icono después actualiza los pedidos EN CURSO (la cocina los está viendo)...
    client.patch("/api/admin/menu/categories/bebidas", json={"icon": "cafe"}, headers=gh)
    active = client.get("/api/orders/active", headers=kds_headers).json()
    assert {i["product_name"]: i["category_icon"] for o in active for i in o["items"]}["Café"] == "cafe"


def test_rename_updates_in_flight_orders_but_not_closed_ones(client, seed, db):
    gh = seed["login_admin"]("admin@gh.com")
    kds_headers, _ = seed["login_kds"]("gh-uvg")

    def order():
        r = client.post(
            "/api/orders/",
            json={"student_id": "20231", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Café", "quantity": 1}]},
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]

    open_id, closed_id = order(), order()
    client.put(f"/api/orders/{closed_id}/status", json={"pipeline_status": "rejected"}, headers=kds_headers)

    r = client.patch("/api/admin/menu/categories/bebidas", json={"name": "Calientes", "icon": "cafe"}, headers=gh)
    assert r.status_code == 200, r.text

    active = {o["id"]: o for o in client.get("/api/orders/active", headers=kds_headers).json()}
    item = active[open_id]["items"][0]
    assert (item["category"], item["category_icon"]) == ("Calientes", "cafe")
    assert closed_id not in active

    from bson import ObjectId

    closed = _run(db.orders.find_one({"_id": ObjectId(closed_id)}))
    assert closed["items"][0]["category"] == "bebidas"  # el pedido cerrado conserva su snapshot


def test_breakfast_meal_period(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    r = client.post(
        "/api/admin/menu",
        json={"name": "Desayuno típico", "category": "desayunos", "price": 30, "meal_period": "desayuno"},
        headers=gh,
    )
    assert r.status_code == 201, r.text
    assert r.json()["meal_period"] == "desayuno"
    bad = client.post(
        "/api/admin/menu",
        json={"name": "X", "category": "desayunos", "price": 3, "meal_period": "merienda"},
        headers=gh,
    )
    assert bad.status_code == 422
