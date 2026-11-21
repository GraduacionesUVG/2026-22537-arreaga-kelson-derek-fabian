"""
Migración de datos para el módulo de administración (idempotente, se puede
correr varias veces):

- admin_users: rol `admin` -> `restaurant_admin` (con restaurant_id) o `superadmin`.
- kds_users: `menu_filter`, `priority`, `active`, `token_version` por defecto.
- restaurants: `opening_hours` al formato de 7 días con `closed`; `service_fee = 0` si no lo tiene
  (la tarifa de cada restaurante se fija con scripts/set_service_fee.py o desde el panel).
- menu_items: `schedule_type`, `code`, `modifiers`, `deleted`.
- orders: `local_date` para pedidos anteriores.
- Índices (scripts/init_db.py).

Uso:
    docker compose exec api python -m scripts.migrate_admin_module
"""

import asyncio
import re
import unicodedata

from motor.motor_asyncio import AsyncIOMotorClient

from scripts.init_db import create_indexes
from src.configuration import settings
from src.core.timeutils import local_date_str
from src.models.admin_user import normalize_admin_role
from src.models.kds_user import MENU_FILTER_ALL
from src.models.menu import resolve_schedule_type
from src.models.restaurant import normalize_opening_hours


def _prefix(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    return (re.sub(r"[^A-Za-z0-9]", "", text).upper()[:4]) or "ITEM"


async def main() -> None:
    client = AsyncIOMotorClient(settings.mongo.uri, tz_aware=True)
    db = client[settings.mongo.db_name]

    admins = 0
    async for admin in db.admin_users.find({}):
        role = normalize_admin_role(admin.get("role"), admin.get("restaurant_id"))
        if role != admin.get("role"):
            await db.admin_users.update_one({"_id": admin["_id"]}, {"$set": {"role": role}})
            admins += 1

    kds = 0
    async for station in db.kds_users.find({}):
        update = {}
        if "menu_filter" not in station:
            update["menu_filter"] = dict(MENU_FILTER_ALL)
        if "priority" not in station:
            update["priority"] = 1
        if "active" not in station:
            update["active"] = True
        if "token_version" not in station:
            update["token_version"] = 0
        if update:
            await db.kds_users.update_one({"_id": station["_id"]}, {"$set": update})
            kds += 1

    restaurants = 0
    async for restaurant in db.restaurants.find({}):
        update = {}
        normalized = normalize_opening_hours(restaurant.get("opening_hours"))
        if normalized != restaurant.get("opening_hours"):
            update["opening_hours"] = normalized
        if "service_fee" not in restaurant:
            update["service_fee"] = 0.0
        if update:
            await db.restaurants.update_one({"_id": restaurant["_id"]}, {"$set": update})
            restaurants += 1

    items = 0
    codes: dict[str, set[str]] = {}
    async for item in db.menu_items.find({}):
        rid = item.get("restaurant_id")
        codes.setdefault(rid, set())
        if item.get("code"):
            codes[rid].add(item["code"])
    async for item in db.menu_items.find({}):
        rid = item.get("restaurant_id")
        update = {}
        schedule = resolve_schedule_type(item)
        if item.get("schedule_type") != schedule:
            update["schedule_type"] = schedule
        if not item.get("code"):
            n = 1
            while f"{_prefix(item.get('name'))}-{n:02d}" in codes[rid]:
                n += 1
            code = f"{_prefix(item.get('name'))}-{n:02d}"
            codes[rid].add(code)
            update["code"] = code
        if "modifiers" not in item:
            update["modifiers"] = []
        if "deleted" not in item:
            update["deleted"] = False
        if update:
            await db.menu_items.update_one({"_id": item["_id"]}, {"$set": update})
            items += 1

    orders = 0
    async for order in db.orders.find({"local_date": {"$exists": False}}):
        if order.get("created_at"):
            await db.orders.update_one({"_id": order["_id"]}, {"$set": {"local_date": local_date_str(order["created_at"])}})
            orders += 1

    await create_indexes(db)
    print(
        f"Migración completa. admin_users: {admins}, kds_users: {kds}, restaurants: {restaurants}, "
        f"menu_items: {items}, orders: {orders}. Índices verificados."
    )
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
