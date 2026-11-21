"""
Carga completa de datos en una sola corrida (conecta directo a MongoDB):

1. Restaurantes Golden Harvest y Gitane (ubicación y horario).
2. Menús de ambos, con código, modificadores, "listo desde", hora límite y porciones.
3. Usuario Dev/TI (superadmin).
4. Un admin por restaurante.
5. Un usuario KDS por restaurante (recibe todo el menú).
6. Ventas simuladas de Golden Harvest para analítica (scripts/seed_analytics_orders.py).
7. Índices.

Es idempotente: si algo ya existe lo actualiza (restablece contraseñas y PIN
a los valores de esta corrida). Con `--reset` borra antes TODAS las
colecciones del proyecto (incluye estudiantes y pedidos) y carga de cero.

Local (Docker):
    docker compose exec api python -m scripts.bootstrap_data --reset

Producción (desde tu máquina, con la URI de Atlas de Render):
    PowerShell:
        $env:MONGO_URI="mongodb+srv://USUARIO:CLAVE@CLUSTER.mongodb.net/"
        $env:MONGO_DB_NAME="gp_agent_db"
        $env:ENVIRONMENT="PRODUCTION"
        uv run python -m scripts.bootstrap_data --reset --allow-production

Opciones: --password (admins y TI), --pin (KDS), --sin-ventas, --dias-ventas N, --yes.
"""

import argparse
import asyncio
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from scripts.init_db import PROJECT_COLLECTIONS, create_indexes
from src.configuration import settings
from src.core.security import hash_password, hash_pin

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _hours(weekday: tuple[str, str], saturday: tuple[str, str] | None) -> dict:
    hours = {d: {"closed": False, "open": weekday[0], "close": weekday[1]} for d in WEEKDAYS[:5]}
    hours["saturday"] = (
        {"closed": False, "open": saturday[0], "close": saturday[1]} if saturday else {"closed": True, "open": None, "close": None}
    )
    hours["sunday"] = {"closed": True, "open": None, "close": None}
    return hours


def _mods(*rows: tuple[str, str, float]) -> list[dict]:
    return [{"id": f"m{i + 1}", "group": g, "name": n, "price": float(p)} for i, (g, n, p) in enumerate(rows)]


def _item(code, name, category, price, prep, **extra) -> dict:
    return {
        "code": code,
        "name": name,
        "category": category,
        "price": float(price),
        "prep_time_minutes": float(prep),
        "description": extra.get("description"),
        "available": True,
        "schedule_type": extra.get("schedule_type", "always"),
        "day_of_week": extra.get("day_of_week"),
        "serve_date": None,
        "option_number": None,
        "meal_period": extra.get("meal_period"),
        "ready_from": extra.get("ready_from"),
        "order_cutoff": extra.get("order_cutoff"),
        "daily_stock": extra.get("daily_stock"),
        "dine_in_only": False,
        "modifiers": extra.get("modifiers", []),
        "is_daily_special": extra.get("schedule_type", "always") != "always",
    }


GH_LUNCH = [
    ("monday", "Bistec, arroz, ensalada, horchata"),
    ("tuesday", "Caldo de pollo, tortillas, fruit punch"),
    ("wednesday", "Pepián, arroz, tortillas, agua de jamaica"),
    ("thursday", "Chile relleno, arroz, ensalada, limonada"),
    ("friday", "Pescado frito, arroz, ensalada, agua de tamarindo"),
]
LUNCH_MODS = _mods(("Quitar", "Sin ensalada", 0), ("Cambios", "Arroz en lugar de papas", 0), ("Extras", "Porción extra de proteína", 12))
DRINK = lambda code, name, price: _item(code, name, "bebidas", price, 1)  # noqa: E731

RESTAURANTS = [
    {
        "name": "Golden Harvest",
        "location": "Campus UVG",
        "opening_hours": _hours(("09:00", "20:00"), ("09:00", "14:00")),
        "admin": {"email": "admin@goldenharvest.com", "username": "Admin Golden Harvest"},
        "kds": "gh-kds",
        "menu": [
            *[
                _item(f"ALM-{day[:3].upper()}", "Almuerzo del día", "almuerzos", 40, 12, description=desc,
                      schedule_type="weekly", day_of_week=day, meal_period="almuerzo", ready_from="11:30",
                      order_cutoff="14:30", daily_stock=60, modifiers=LUNCH_MODS)
                for day, desc in GH_LUNCH
            ],
            _item("CENA-01", "Cena", "cenas", 20, 10, meal_period="cena", ready_from="17:00", order_cutoff="19:30", daily_stock=25),
            _item("BAGU-01", "Baguette chapin", "comida", 20, 6,
                  modifiers=_mods(("Quitar", "Sin cebolla", 0), ("Extras", "Extra queso", 4), ("Extras", "Aguacate", 5))),
            _item("HAMB-01", "Hamburguesa", "comida", 25, 8,
                  modifiers=_mods(("Quitar", "Sin tomate", 0), ("Quitar", "Sin cebolla", 0), ("Extras", "Doble queso", 5), ("Extras", "Tocino", 6))),
            _item("SUSH-01", "Sushi california roll", "sushi", 35, 10, daily_stock=40,
                  modifiers=_mods(("Quitar", "Sin queso crema", 0), ("Extras", "Salsa de anguila", 3), ("Extras", "Tempura", 5))),
            _item("SUSH-02", "Sushi dragon roll", "sushi", 40, 12, daily_stock=30,
                  modifiers=_mods(("Quitar", "Sin picante", 0), ("Extras", "Salsa de anguila", 3))),
            DRINK("PEPS-01", "Pepsi lata 355ml", 8),
            DRINK("PEPS-02", "Pepsi botella 625ml", 10),
            DRINK("PEPS-03", "Pepsi Black lata 355ml", 8),
            DRINK("JUGO-01", "Jugo Petit Durazno 330ml", 8),
            DRINK("JUGO-02", "Jugo Petit Manzana 330ml", 8),
        ],
    },
    {
        "name": "Gitane",
        "location": "Campus UVG",
        "opening_hours": _hours(("09:00", "20:00"), ("09:00", "14:00")),
        "admin": {"email": "admin@gitane.com", "username": "Admin Gitane"},
        "kds": "gitane-kds",
        "menu": [
            _item("ATRE-01", "Atrevido", "especialidades", 35, 10,
                  modifiers=_mods(("Quitar", "Sin cebolla", 0), ("Extras", "Extra queso", 5))),
            _item("PAPA-01", "Papas con queso", "papas", 16, 6, modifiers=_mods(("Extras", "Tocino", 5))),
            _item("PAPA-02", "Papas con salsa", "papas", 16, 6, modifiers=_mods(("Extras", "Salsa extra", 2))),
            _item("SALC-01", "Salchipapas con queso", "papas", 19, 7, modifiers=_mods(("Extras", "Tocino", 5))),
            _item("SALC-02", "Salchipapas con salsa", "papas", 19, 7, modifiers=_mods(("Extras", "Salsa extra", 2))),
            DRINK("COCA-01", "Coca-Cola", 11),
            _item("GRAN-01", "Granita café", "bebidas", 19, 3, modifiers=_mods(("Extras", "Crema batida", 3))),
            _item("GRAN-02", "Granita Fresa", "bebidas", 19, 3, modifiers=_mods(("Extras", "Crema batida", 3))),
            _item("CAFE-01", "Café Americano", "bebidas", 12, 2, modifiers=_mods(("Leche", "Leche deslactosada", 0), ("Extras", "Shot extra", 5))),
            _item("CHEE-01", "Cheesecake Fresa", "postres", 16, 2, daily_stock=20),
            _item("CHEE-02", "Cheesecake Cajeta", "postres", 16, 2, daily_stock=20),
        ],
    },
]
TI_ADMIN = {"email": "ti@uvg.edu.gt", "username": "Dev/TI"}


def _mask(uri: str) -> str:
    if "@" in uri:
        head, tail = uri.split("@", 1)
        return head.split("//")[0] + "//***@" + tail
    return uri


async def main(args) -> None:
    production = settings.environment.upper() == "PRODUCTION"
    if production and not args.allow_production:
        raise SystemExit("ENVIRONMENT=PRODUCTION: agrega --allow-production para cargar datos en esta base.")

    print(f"Base: {_mask(settings.mongo.uri)}  db='{settings.mongo.db_name}'  ENVIRONMENT={settings.environment}")
    client = AsyncIOMotorClient(settings.mongo.uri, tz_aware=True, serverSelectionTimeoutMS=15000)
    db = client[settings.mongo.db_name]
    try:
        await client.admin.command("ping")
    except Exception as exc:
        client.close()
        raise SystemExit(f"No se pudo conectar a MongoDB ({exc}). Revisa MONGO_URI y que tu IP esté permitida en Atlas.")

    if args.reset:
        if not args.yes:
            print(f"\n--reset BORRA estas colecciones en '{settings.mongo.db_name}': {', '.join(PROJECT_COLLECTIONS)}")
            if input("Escribe BORRAR para continuar: ").strip() != "BORRAR":
                client.close()
                raise SystemExit("Cancelado, no se tocó nada.")
        for name in PROJECT_COLLECTIONS:
            await db[name].drop()
        print("Colecciones borradas.")

    await create_indexes(db)
    now = datetime.now(timezone.utc)
    summary = []

    for data in RESTAURANTS:
        restaurant = await db.restaurants.find_one_and_update(
            {"name": data["name"]},
            {"$set": {"name": data["name"], "location": data["location"], "active": True, "opening_hours": data["opening_hours"]},
             "$setOnInsert": {"created_at": now}},
            upsert=True,
            return_document=True,
        )
        rid = str(restaurant["_id"])

        codes = [item["code"] for item in data["menu"]]
        for item in data["menu"]:
            await db.menu_items.update_one(
                {"restaurant_id": rid, "code": item["code"]},
                {"$set": {**item, "restaurant_id": rid, "deleted": False, "deleted_at": None, "updated_at": now},
                 "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        # Productos anteriores sin código de esta carga: se marcan como borrados para no duplicar nombres.
        await db.menu_items.update_many(
            {"restaurant_id": rid, "code": {"$nin": codes}, "deleted": {"$ne": True}},
            {"$set": {"deleted": True, "deleted_at": now}},
        )

        await db.admin_users.update_one(
            {"email": data["admin"]["email"]},
            {"$set": {"username": data["admin"]["username"], "password_hash": hash_password(args.password),
                      "restaurant_id": rid, "restaurant_name": data["name"], "role": "restaurant_admin",
                      "must_change_password": False},
             "$setOnInsert": {"email": data["admin"]["email"], "created_at": now, "last_login_at": None}},
            upsert=True,
        )

        # Un solo KDS por restaurante: las demás estaciones del restaurante quedan pausadas.
        await db.kds_users.update_many({"restaurant_id": rid, "username": {"$ne": data["kds"]}}, {"$set": {"active": False}})
        await db.kds_users.update_one(
            {"username": data["kds"]},
            {"$set": {"pin_hash": hash_pin(args.pin), "restaurant_id": rid, "restaurant_name": data["name"],
                      "menu_filter": {"mode": "all", "item_ids": [], "categories": []}, "priority": 1, "active": True},
             "$inc": {"token_version": 1},
             "$setOnInsert": {"username": data["kds"], "created_at": now, "last_login_at": None}},
            upsert=True,
        )
        summary.append((data["name"], rid, len(data["menu"]), data["admin"]["email"], data["kds"]))

    await db.admin_users.update_one(
        {"email": TI_ADMIN["email"]},
        {"$set": {"username": TI_ADMIN["username"], "password_hash": hash_password(args.password), "restaurant_id": None,
                  "restaurant_name": None, "role": "superadmin", "must_change_password": False},
         "$setOnInsert": {"email": TI_ADMIN["email"], "created_at": now, "last_login_at": None}},
        upsert=True,
    )
    client.close()

    print("\nListo:")
    for name, rid, items, admin, kds in summary:
        print(f"  {name:<15} id={rid}  productos={items}")
        print(f"      admin: {admin} / {args.password}")
        print(f"      KDS:   {kds} / PIN {args.pin}")
    print(f"  Dev/TI:  {TI_ADMIN['email']} / {args.password}")

    if args.sin_ventas:
        return
    print("\nGenerando ventas de Golden Harvest...")
    from scripts import seed_analytics_orders

    await seed_analytics_orders.main(
        argparse.Namespace(
            days=args.dias_ventas, include_today=False, min_per_day=50, max_per_day=80, saturday_min=20,
            saturday_max=35, kds_username="gh-kds", seed=2026, keep=False, clear=False, no_events=False,
            allow_production=args.allow_production,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="Borra todas las colecciones del proyecto antes de cargar")
    parser.add_argument("--yes", action="store_true", help="No pide confirmación con --reset")
    parser.add_argument("--password", default="Password123", help="Contraseña de los admins y de Dev/TI")
    parser.add_argument("--pin", default="1234", help="PIN de los KDS")
    parser.add_argument("--sin-ventas", action="store_true", help="No genera ventas simuladas")
    parser.add_argument("--dias-ventas", type=int, default=30)
    parser.add_argument("--allow-production", action="store_true")
    asyncio.run(main(parser.parse_args()))
