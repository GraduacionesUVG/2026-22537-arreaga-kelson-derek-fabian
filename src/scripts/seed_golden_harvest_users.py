"""
Crea (o actualiza) los usuarios de prueba de Golden Harvest, sin preguntas:

- Admin del restaurante: admin@goldenharvest.com / Password123
- KDS gh-uvg (PIN 1234): cocina UVG, recibe todo menos almuerzos
  (categorías comida, sushi, bebidas, cenas).
- KDS gh-spazio (PIN 1234): cocina Spazio, recibe solo almuerzos
  (categorías almuerzos y almuerzos del día).

Requiere que el restaurante exista (scripts/seed_menu_test_data.py). Es
idempotente: si los usuarios ya existen, restablece su contraseña/PIN y su
configuración de estación.

    docker compose exec api python -m scripts.seed_golden_harvest_users
    docker compose exec api python -m scripts.seed_golden_harvest_users --password OtraClave --pin 4321
    docker compose exec api python -m scripts.seed_golden_harvest_users --pausar-otras

Si Golden Harvest ya tiene otras estaciones KDS (ej. una creada a mano que
recibe todo el menú), las solicitudes se pueden repartir entre ellas y
gh-uvg. `--pausar-otras` las deja inactivas (no reciben solicitudes nuevas);
se reactivan desde el panel (PUT /api/admin/kds-users/{id}/active).
"""

import argparse
import asyncio
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.core.security import hash_password, hash_pin

RESTAURANT_NAME = "Golden Harvest"
ADMIN_EMAIL = "admin@goldenharvest.com"
STATIONS = [
    {
        "username": "gh-uvg",
        "priority": 1,
        "menu_filter": {"mode": "custom", "item_ids": [], "categories": ["comida", "sushi", "bebidas", "cenas"]},
    },
    {
        "username": "gh-spazio",
        "priority": 1,
        "menu_filter": {"mode": "custom", "item_ids": [], "categories": ["almuerzos", "almuerzos del día"]},
    },
]


async def main(password: str, pin: str, pause_others: bool = False) -> None:
    client = AsyncIOMotorClient(settings.mongo.uri, tz_aware=True)
    db = client[settings.mongo.db_name]
    now = datetime.now(timezone.utc)

    restaurant = await db.restaurants.find_one({"name": RESTAURANT_NAME})
    if restaurant is None:
        client.close()
        raise SystemExit(f"No existe '{RESTAURANT_NAME}'. Corre primero: python -m scripts.seed_menu_test_data")
    restaurant_id = str(restaurant["_id"])

    await db.admin_users.update_one(
        {"email": ADMIN_EMAIL},
        {
            "$set": {
                "username": "Admin Golden Harvest",
                "password_hash": hash_password(password),
                "restaurant_id": restaurant_id,
                "restaurant_name": RESTAURANT_NAME,
                "role": "restaurant_admin",
                "must_change_password": False,
            },
            "$setOnInsert": {"email": ADMIN_EMAIL, "created_at": now, "last_login_at": None},
        },
        upsert=True,
    )

    for station in STATIONS:
        await db.kds_users.update_one(
            {"username": station["username"]},
            {
                "$set": {
                    "pin_hash": hash_pin(pin),
                    "restaurant_id": restaurant_id,
                    "restaurant_name": RESTAURANT_NAME,
                    "menu_filter": station["menu_filter"],
                    "priority": station["priority"],
                    "active": True,
                },
                "$inc": {"token_version": 1},
                "$setOnInsert": {"username": station["username"], "created_at": now, "last_login_at": None},
            },
            upsert=True,
        )

    own = {s["username"] for s in STATIONS}
    others = [
        s async for s in db.kds_users.find({"restaurant_id": restaurant_id, "username": {"$nin": list(own)}})
    ]
    active_others = [s for s in others if s.get("active", True)]
    if pause_others and active_others:
        await db.kds_users.update_many(
            {"_id": {"$in": [s["_id"] for s in active_others]}}, {"$set": {"active": False}}
        )

    print(f"{RESTAURANT_NAME} (restaurant_id={restaurant_id})")
    print(f"  Admin: {ADMIN_EMAIL} / {password}")
    for station in STATIONS:
        print(f"  KDS:   {station['username']} / PIN {pin}  -> {', '.join(station['menu_filter']['categories'])}")
    if active_others and pause_others:
        print(f"  Estaciones pausadas: {', '.join(s['username'] for s in active_others)}")
    elif active_others:
        print(
            f"  (!) Otras estaciones activas en {RESTAURANT_NAME}: {', '.join(s['username'] for s in active_others)}. "
            "Pueden recibir las solicitudes de la simulación; usa --pausar-otras para dejarlas inactivas."
        )
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", default="Password123")
    parser.add_argument("--pin", default="1234")
    parser.add_argument("--pausar-otras", action="store_true", help="Deja inactivas las demás estaciones KDS de Golden Harvest")
    args = parser.parse_args()
    asyncio.run(main(args.password, args.pin, args.pausar_otras))
