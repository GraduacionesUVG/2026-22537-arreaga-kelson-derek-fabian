"""
Puebla `restaurants` y `menu_items` con datos de prueba (Golden Harvest, Gitane).

Modos:
  --mode append   (default) upsert por (restaurant_id, name[, day_of_week]): agrega
                  productos nuevos y actualiza los existentes, sin tocar el resto del
                  catálogo del restaurante. Simula un CSV que solo agrega/actualiza.
  --mode replace  borra el catálogo actual de cada restaurante definido acá
                  antes de insertarlo. Simula un CSV que reemplaza el catálogo completo.

Los restaurantes se identifican por `name` (no hay índice unique en Mongo
para restaurants todavía, pero para seeding es una llave natural suficiente).

Uso (correr como módulo, no como archivo suelto: src/ solo se resuelve si
/code o la raíz del proyecto está en sys.path, y eso solo pasa con `-m`):
    uv run python -m scripts.seed_menu_test_data --mode append
    uv run python -m scripts.seed_menu_test_data --mode replace

Dentro de Docker:
    docker compose exec api python -m scripts.seed_menu_test_data --mode append
"""

import argparse
import asyncio
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from src.configuration import settings
from src.models.menu import COLLECTION_NAME as MENU_COLLECTION
from src.models.menu import MealPeriod
from src.models.restaurant import COLLECTION_NAME as RESTAURANT_COLLECTION

# Todos los restaurantes del piloto comparten el mismo horario de atención.
DEFAULT_OPENING_HOURS = {
    "monday": {"open": "09:00", "close": "20:00"},
    "tuesday": {"open": "09:00", "close": "20:00"},
    "wednesday": {"open": "09:00", "close": "20:00"},
    "thursday": {"open": "09:00", "close": "20:00"},
    "friday": {"open": "09:00", "close": "20:00"},
    "saturday": {"open": "09:00", "close": "14:00"},
    "sunday": None,
}

RESTAURANTS = [
    {
        "name": "Golden Harvest",
        "location": "Campus UVG",
        "active": True,
        "service_fee": 2.65,  # Q por pedido, se le suma al cliente
        "opening_hours": DEFAULT_OPENING_HOURS,
    },
    {
        "name": "Gitane",
        "location": "Campus UVG",
        "active": True,
        "service_fee": 0.0,
        "opening_hours": DEFAULT_OPENING_HOURS,
    },
]

# Almuerzo del día: un documento POR DÍA de la semana (mismo `name`, distinto
# `day_of_week`/`description`) -- MenuService solo devuelve/deja ordenar el de
# hoy (ver src/services/menu_service.py::filter_available_today). El
# restaurante sube esto cada semana; acá es solo una muestra de una semana.
GOLDEN_HARVEST_WEEK_LUNCH = [
    {"day_of_week": "monday", "description": "Bistec, arroz, ensalada, horchata"},
    {"day_of_week": "tuesday", "description": "Caldo de pollo, tortillas, fruit punch"},
    {"day_of_week": "wednesday", "description": "Pepián, arroz, tortillas, agua de jamaica"},
    {"day_of_week": "thursday", "description": "Chile relleno, arroz, ensalada, limonada"},
    {"day_of_week": "friday", "description": "Pescado frito, arroz, ensalada, agua de tamarindo"},
]

MENU_ITEMS = {
    "Golden Harvest": [
        *[
            {
                "name": "Almuerzo del día",
                "category": "almuerzos",
                "price": 40.0,
                "meal_period": MealPeriod.LUNCH,
                "is_daily_special": True,
                "day_of_week": day["day_of_week"],
                "description": day["description"],
                "prep_time_minutes": 12,
            }
            for day in GOLDEN_HARVEST_WEEK_LUNCH
        ],
        {
            "name": "Cena",
            "category": "cenas",
            "price": 20.0,
            "meal_period": MealPeriod.DINNER,
            "prep_time_minutes": 10,
        },
        {"name": "Baguette chapin", "category": "comida", "price": 20.0, "prep_time_minutes": 6},
        {"name": "Hamburguesa", "category": "comida", "price": 25.0, "prep_time_minutes": 8},
        {"name": "Sushi california roll", "category": "sushi", "price": 35.0, "prep_time_minutes": 10},
        {"name": "Sushi dragon roll", "category": "sushi", "price": 40.0, "prep_time_minutes": 12},
        {"name": "Pepsi lata 355ml", "category": "bebidas", "price": 8.0, "prep_time_minutes": 1},
        {"name": "Pepsi botella 625ml", "category": "bebidas", "price": 10.0, "prep_time_minutes": 1},
        {"name": "Pepsi Black lata 355ml", "category": "bebidas", "price": 8.0, "prep_time_minutes": 1},
        {"name": "Jugo Petit Durazno 330ml", "category": "bebidas", "price": 8.0, "prep_time_minutes": 1},
        {"name": "Jugo Petit Manzana 330ml", "category": "bebidas", "price": 8.0, "prep_time_minutes": 1},
    ],
    "Gitane": [
        {"name": "Atrevido", "category": "especialidades", "price": 35.0, "prep_time_minutes": 10},
        {"name": "Papas con queso", "category": "papas", "price": 16.0, "prep_time_minutes": 6},
        {"name": "Papas con salsa", "category": "papas", "price": 16.0, "prep_time_minutes": 6},
        {"name": "Salchipapas con queso", "category": "papas", "price": 19.0, "prep_time_minutes": 7},
        {"name": "Salchipapas con salsa", "category": "papas", "price": 19.0, "prep_time_minutes": 7},
        {"name": "Coca-Cola", "category": "bebidas", "price": 11.0, "prep_time_minutes": 1},
        {"name": "Granita café", "category": "bebidas", "price": 19.0, "prep_time_minutes": 3},
        {"name": "Granita Fresa", "category": "bebidas", "price": 19.0, "prep_time_minutes": 3},
        {"name": "Café Americano", "category": "bebidas", "price": 12.0, "prep_time_minutes": 2},
        {"name": "Cheesecake Fresa", "category": "postres", "price": 16.0, "prep_time_minutes": 2},
        {"name": "Cheesecake Cajeta", "category": "postres", "price": 16.0, "prep_time_minutes": 2},
    ],
}


def _with_defaults(item: dict) -> dict:
    """Rellena los campos opcionales del schema que no todos los items usan."""
    defaults = {
        "available": True,
        "meal_period": None,
        "is_daily_special": False,
        "day_of_week": None,
        "description": None,
        "prep_time_minutes": 5.0,
        "dine_in_only": False,
    }
    merged = defaults | item
    if isinstance(merged["meal_period"], MealPeriod):
        merged["meal_period"] = merged["meal_period"].value
    return merged


async def upsert_restaurant(db, data: dict) -> str:
    now = datetime.now(timezone.utc)
    result = await db[RESTAURANT_COLLECTION].find_one_and_update(
        {"name": data["name"]},
        {"$set": data, "$setOnInsert": {"created_at": now}},
        upsert=True,
        return_document=True,
    )
    return str(result["_id"])


async def sync_menu_items(db, restaurant_id: str, items: list[dict], mode: str) -> None:
    collection = db[MENU_COLLECTION]
    now = datetime.now(timezone.utc)

    if mode == "replace":
        await collection.delete_many({"restaurant_id": restaurant_id})

    for raw_item in items:
        item = _with_defaults(raw_item) | {"restaurant_id": restaurant_id}
        # `day_of_week` entra a la llave de upsert: el almuerzo del día sube 5
        # documentos con el mismo `name` pero un `day_of_week` distinto cada uno.
        await collection.update_one(
            {"restaurant_id": restaurant_id, "name": item["name"], "day_of_week": item["day_of_week"]},
            {"$set": item, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )


async def main(mode: str) -> None:
    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]

    for restaurant in RESTAURANTS:
        restaurant_id = await upsert_restaurant(db, restaurant)
        items = MENU_ITEMS[restaurant["name"]]
        await sync_menu_items(db, restaurant_id, items, mode)
        print(f"{restaurant['name']}: {len(items)} productos ({mode}) -> restaurant_id={restaurant_id}")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["append", "replace"], default="append")
    args = parser.parse_args()
    asyncio.run(main(args.mode))
