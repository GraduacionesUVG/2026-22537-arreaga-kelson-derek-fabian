"""Fixtures de pruebas: MongoDB en memoria (mongomock-motor) y cliente HTTP sin lifespan."""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("KAFKA_ENABLED", "false")
# Los avisos por WhatsApp en proceso se prueban aparte (test_customer_notifications.py).
os.environ.setdefault("NOTIFY_INLINE_WITHOUT_KAFKA", "false")
os.environ.setdefault("RESEND_API_KEY", "")
# Sin corte de "último pedido" en los tests generales (los restaurantes de prueba abren todo el
# día y no deben fallar según la hora a la que corren). Se prueba aparte con monkeypatch.
os.environ.setdefault("LAST_ORDER_MINUTES_BEFORE_CLOSE", "0")
# Sin pedido mínimo en los tests generales (muchos piden un solo producto barato).
# Se prueba aparte en tests/test_min_order_amount.py con monkeypatch.
os.environ.setdefault("MIN_ORDER_AMOUNT", "0")
# Orígenes de los frontends para tests/test_cors.py (main.py los lee al importarse).
os.environ.setdefault("CORS_ORIGINS", "https://qorder-admin.onrender.com,https://qorder-kds.onrender.com")

from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from main import app
from src.configuration import settings
from src.core.database import get_db, mongo
from src.core.realtime import kds_manager
from src.core.security import hash_password, hash_pin

settings.email.resend_api_key = ""
# Las rutas del agente exigen X-Internal-Api-Key: el cliente de pruebas lo manda siempre
# (cada test puede sobrescribirlo con headers={...}). Ver tests/test_api_security.py.
INTERNAL_KEY = "test-internal-key"
settings.auth.internal_api_key = INTERNAL_KEY

ALWAYS_OPEN = {
    day: {"closed": False, "open": "00:00", "close": "23:59"}
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


@pytest.fixture()
def db():
    client = AsyncMongoMockClient(tz_aware=True)
    database = client["gp_test"]
    mongo.db = database
    app.dependency_overrides[get_db] = lambda: database
    kds_manager.__init__()
    yield database
    app.dependency_overrides.clear()
    mongo.db = None


@pytest.fixture()
def client(db):
    return TestClient(app, headers={"X-Internal-Api-Key": INTERNAL_KEY})


def run(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def seed(db, client):
    """Dos restaurantes, un admin por restaurante, un superadmin y estaciones KDS."""
    import asyncio

    loop = asyncio.new_event_loop()

    async def _seed():
        now = datetime.now(timezone.utc)
        gh = await db.restaurants.insert_one(
            {"name": "Golden Harvest", "location": "Edificio A", "active": True, "opening_hours": ALWAYS_OPEN}
        )
        gi = await db.restaurants.insert_one(
            # Abierto siempre: los pedidos fuera de horario se rechazan (409) y los tests no deben
            # depender de la hora a la que corren. El horario real se prueba en test_restaurant_availability.py.
            {"name": "Gitane", "location": "CIT", "active": True, "opening_hours": ALWAYS_OPEN}
        )
        gh_id, gi_id = str(gh.inserted_id), str(gi.inserted_id)
        await db.admin_users.insert_many(
            [
                {"email": "admin@gh.com", "username": "Admin GH", "password_hash": hash_password("secret123"),
                 "restaurant_id": gh_id, "restaurant_name": "Golden Harvest", "role": "admin",
                 "must_change_password": False, "created_at": now},
                {"email": "admin@gitane.com", "username": "Admin Gitane", "password_hash": hash_password("secret123"),
                 "restaurant_id": gi_id, "restaurant_name": "Gitane", "role": "restaurant_admin",
                 "must_change_password": False, "created_at": now},
                {"email": "ti@uvg.com", "username": "TI", "password_hash": hash_password("secret123"),
                 "restaurant_id": None, "restaurant_name": None, "role": "superadmin",
                 "must_change_password": False, "created_at": now},
            ]
        )
        await db.kds_users.insert_many(
            [
                {"username": "gh-uvg", "pin_hash": hash_pin("1234"), "restaurant_name": "Golden Harvest",
                 "restaurant_id": gh_id, "menu_filter": {"mode": "custom", "item_ids": [], "categories": ["comida", "bebidas"]},
                 "priority": 1, "active": True, "token_version": 0, "created_at": now},
                {"username": "gh-spazio", "pin_hash": hash_pin("1234"), "restaurant_name": "Golden Harvest",
                 "restaurant_id": gh_id, "menu_filter": {"mode": "custom", "item_ids": [], "categories": ["almuerzos del día"]},
                 "priority": 1, "active": True, "token_version": 0, "created_at": now},
                {"username": "gitane1", "pin_hash": hash_pin("1234"), "restaurant_name": "Gitane",
                 "restaurant_id": gi_id, "created_at": now},
            ]
        )
        await db.menu_items.insert_many(
            [
                {"restaurant_id": gh_id, "code": "HAMB-01", "name": "Hamburguesa", "category": "comida", "price": 25.0,
                 "available": True, "prep_time_minutes": 8,
                 "modifiers": [{"id": "m1", "group": "Extras", "name": "Doble queso", "price": 5.0},
                               {"id": "m2", "group": "Quitar", "name": "Sin tomate", "price": 0.0}]},
                {"restaurant_id": gh_id, "code": "CAFE-01", "name": "Café", "category": "bebidas", "price": 10.0,
                 "available": True, "prep_time_minutes": 2},
                {"restaurant_id": gi_id, "code": "PAPA-01", "name": "Papas", "category": "comida", "price": 15.0,
                 "available": True, "prep_time_minutes": 5},
            ]
        )
        return gh_id, gi_id

    gh_id, gi_id = loop.run_until_complete(_seed())
    loop.close()

    def login_admin(email):
        r = client.post("/auth/admin/login", json={"email": email, "password": "secret123"})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def login_kds(username, pin="1234"):
        r = client.post("/auth/kds/login", json={"username": username, "pin": pin})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()

    return {
        "gh_id": gh_id,
        "gi_id": gi_id,
        "login_admin": login_admin,
        "login_kds": login_kds,
    }
