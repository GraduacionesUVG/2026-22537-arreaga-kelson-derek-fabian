"""
Crea (o deja al día) un RESTAURANTE DE PRUEBA completo para demos, directamente
en la base configurada en `.env` (MONGO_URI / MONGO_DB_NAME):

- Restaurante abierto todos los días de 00:00 a 23:59 (se puede pedir de noche,
  hasta LAST_ORDER_MINUTES_BEFORE_CLOSE antes del cierre) y sin cuenta de Tilopay
  (con SIMULATED_PAYMENTS_ENABLED=true el pago se confirma con /webhooks/bac-payment).
- Admin del restaurante (correo + contraseña, sin cambio obligatorio de contraseña).
- Una estación KDS (usuario + PIN) que recibe todo el menú. Queda INACTIVA: se activa
  al iniciar turno desde el KDS (POST /api/kds/shift/start), como en la operación real.
- Menú de ejemplo con comida y bebidas (precios por encima del pedido mínimo).
- Opcional: un usuario Dev/TI (superadmin) para mostrar el panel de OPS (--ops-email).

Es idempotente: si el restaurante, el admin o la estación ya existen, los actualiza
(contraseña, PIN, horario, restaurante asignado). El menú solo se crea si el
restaurante no tiene productos, o se reemplaza con --reset-menu.

Se corre como módulo desde la raíz del repo:

    uv run python -m scripts.seed_demo_restaurant
    uv run python -m scripts.seed_demo_restaurant --admin-email demo@qorder.lat --admin-password "OtraClave123"
    uv run python -m scripts.seed_demo_restaurant --ops-email ops.demo@qorder.lat --ops-password "ClaveOps123"
    uv run python -m scripts.seed_demo_restaurant --reset-menu --yes

Dentro de Docker: docker compose exec api python -m scripts.seed_demo_restaurant
"""

import argparse
import asyncio
import re
import secrets
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.core.database import redact_uri
from src.core.security import hash_password, hash_pin

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ALWAYS_OPEN = {day: {"closed": False, "open": "00:00", "close": "23:59"} for day in WEEKDAYS}

# (código, nombre, categoría, precio, minutos de preparación, descripción, modificadores [(nombre, precio)])
DEMO_MENU = [
    ("DEMO-POLLO", "Pollo a la plancha", "comida", 32.0, 9, "Con arroz y ensalada.",
     [("Extra aguacate", 6.0), ("Sin cebolla", 0.0)]),
    ("DEMO-HAMB", "Hamburguesa clásica", "comida", 28.0, 8, "Carne de res, queso y papas.",
     [("Doble queso", 5.0), ("Sin tomate", 0.0)]),
    ("DEMO-WRAP", "Wrap de pollo", "comida", 26.0, 6, "Tortilla de harina, pollo y vegetales.", []),
    ("DEMO-ENSA", "Ensalada César", "comida", 25.0, 5, "Lechuga, crutones y aderezo César.",
     [("Con pollo", 8.0)]),
    ("DEMO-LIMO", "Limonada natural", "bebidas", 10.0, 2, "16 oz.", []),
    ("DEMO-CAFE", "Café americano", "bebidas", 9.0, 2, "12 oz.", []),
]

PIN_PATTERN = re.compile(r"^\d{4,8}$")


def menu_document(restaurant_id: str, row: tuple, now: datetime) -> dict:
    """Mismos campos que crea el panel (ver src/services/ti_service.py y menu_service.normalize_item)."""
    code, name, category, price, prep, description, modifiers = row
    return {
        "restaurant_id": restaurant_id,
        "code": code,
        "name": name,
        "category": category,
        "description": description,
        "price": price,
        "available": True,
        "schedule_type": "always",
        "day_of_week": None,
        "serve_date": None,
        "option_number": None,
        "meal_period": None,
        "prep_time_minutes": float(prep),
        "ready_from": None,
        "order_cutoff": None,
        "daily_stock": None,
        "dine_in_only": False,
        "modifiers": [
            {"id": secrets.token_hex(6), "group": "Opciones", "name": m_name, "price": m_price}
            for m_name, m_price in modifiers
        ],
        "is_daily_special": False,
        "deleted": False,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }


def validate(args: argparse.Namespace) -> None:
    if not PIN_PATTERN.match(args.kds_pin):
        raise SystemExit("El PIN del KDS debe tener entre 4 y 8 dígitos.")
    if len(args.admin_password) < 8:
        raise SystemExit("La contraseña del admin debe tener al menos 8 caracteres.")
    if args.ops_email and (not args.ops_password or len(args.ops_password) < 8):
        raise SystemExit("Con --ops-email hay que pasar --ops-password (mínimo 8 caracteres).")


async def main(args: argparse.Namespace) -> None:
    validate(args)
    print(f"Base de datos: {redact_uri(settings.mongo.uri)}  db='{settings.mongo.db_name}'  "
          f"(ENVIRONMENT={settings.environment})")
    if not args.yes:
        answer = input(f"Se va a crear/actualizar '{args.name}' en esa base. ¿Continuar? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            raise SystemExit("Cancelado.")

    client = AsyncIOMotorClient(settings.mongo.uri, tz_aware=True)
    try:
        restaurant_id = await seed_demo(client[settings.mongo.db_name], args)
    finally:
        client.close()

    print("\nCredenciales para la demo (demo/demo_server.py → ⚙ Configurar):")
    print(f"  restaurant_id : {restaurant_id}")
    print(f"  Admin         : {args.admin_email.strip().lower()} / {args.admin_password}")
    print(f"  KDS           : {args.kds_username} / PIN {args.kds_pin}")
    if args.ops_email:
        print(f"  Dev/TI (OPS)  : {args.ops_email.strip().lower()} / {args.ops_password}")


async def seed_demo(db, args: argparse.Namespace) -> str:
    """Crea/actualiza todo en `db` y devuelve el id del restaurante (lo reutiliza demo/dev_backend.py)."""
    now = datetime.now(timezone.utc)
    # --- Restaurante -------------------------------------------------------------------
    restaurant_fields = {
        "location": args.location,
        "active": True,
        "opening_hours": ALWAYS_OPEN,
        "service_fee": round(args.service_fee, 2),
    }
    existing = await db.restaurants.find_one({"name": args.name})
    if existing:
        await db.restaurants.update_one({"_id": existing["_id"]}, {"$set": restaurant_fields})
        restaurant_id = str(existing["_id"])
        print(f"Restaurante actualizado: {args.name} ({restaurant_id})")
    else:
        result = await db.restaurants.insert_one({"name": args.name, **restaurant_fields, "created_at": now})
        restaurant_id = str(result.inserted_id)
        print(f"Restaurante creado:      {args.name} ({restaurant_id})")

    # --- Admin del restaurante -------------------------------------------------------------
    admin_email = args.admin_email.strip().lower()
    await db.admin_users.update_one(
        {"email": admin_email},
        {
            "$set": {
                "username": f"Admin {args.name}",
                "password_hash": hash_password(args.admin_password),
                "restaurant_id": restaurant_id,
                "restaurant_name": args.name,
                "role": "restaurant_admin",
                "must_change_password": False,
                "password_changed_at": now,
            },
            "$setOnInsert": {"email": admin_email, "created_at": now, "last_login_at": None},
        },
        upsert=True,
    )
    print(f"Admin listo:             {admin_email}")

    # --- Estación KDS ------------------------------------------------------------------------
    prior = await db.kds_users.find_one({"username": args.kds_username})
    await db.kds_users.update_one(
        {"username": args.kds_username},
        {
            "$set": {
                "pin_hash": hash_pin(args.kds_pin),
                "restaurant_id": restaurant_id,
                "restaurant_name": args.name,
                "menu_filter": {"mode": "all", "item_ids": [], "categories": []},
                "priority": 1,
                # Inactiva hasta que el KDS inicia turno (POST /api/kds/shift/start).
                "active": False,
                # Invalida tokens viejos si la estación existía (ej. de otro restaurante).
                "token_version": int((prior or {}).get("token_version", 0)) + (1 if prior else 0),
            },
            "$setOnInsert": {"username": args.kds_username, "created_at": now},
        },
        upsert=True,
    )
    others = await db.kds_users.count_documents(
        {"restaurant_id": restaurant_id, "username": {"$ne": args.kds_username}, "active": True}
    )
    print(f"Estación KDS lista:      {args.kds_username} (recibe todo el menú)")
    if others:
        print(f"  Aviso: el restaurante tiene {others} estación(es) activa(s) más; podrían recibir los pedidos.")

    # --- Menú -----------------------------------------------------------------------------------
    current = await db.menu_items.count_documents({"restaurant_id": restaurant_id, "deleted": {"$ne": True}})
    if args.reset_menu and current:
        await db.menu_items.delete_many({"restaurant_id": restaurant_id})
        current = 0
    if current == 0:
        await db.menu_items.insert_many([menu_document(restaurant_id, row, now) for row in DEMO_MENU])
        print(f"Menú creado:             {len(DEMO_MENU)} productos")
    else:
        print(f"Menú existente:          {current} productos (usa --reset-menu para reemplazarlo)")

    # --- Usuario Dev/TI (opcional) -------------------------------------------------------------------
    if args.ops_email:
        ops_email = args.ops_email.strip().lower()
        await db.admin_users.update_one(
            {"email": ops_email},
            {
                "$set": {
                    "username": "Dev/TI Demo",
                    "password_hash": hash_password(args.ops_password),
                    "restaurant_id": None,
                    "restaurant_name": None,
                    "role": "superadmin",
                    "must_change_password": False,
                    "password_changed_at": now,
                },
                "$setOnInsert": {"email": ops_email, "created_at": now, "last_login_at": None},
            },
            upsert=True,
        )
        print(f"Dev/TI (superadmin):     {ops_email}")
    return restaurant_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="Local de Prueba", help="Nombre del restaurante (único).")
    parser.add_argument("--location", default="CIT Nivel 1 A la par de la plaza")
    parser.add_argument("--service-fee", type=float, default=2.50, help="Tarifa de servicio por pedido (Q).")
    parser.add_argument("--admin-email", default="admin@localdeprueba.com")
    parser.add_argument("--admin-password", default="Demo2026!")
    parser.add_argument("--kds-username", default="lp-cocina")
    parser.add_argument("--kds-pin", default="2468")
    parser.add_argument("--ops-email", default=None, help="Crea/actualiza un superadmin Dev/TI para el panel de OPS.")
    parser.add_argument("--ops-password", default=None)
    parser.add_argument("--reset-menu", action="store_true", help="Borra el menú del restaurante y crea el de ejemplo.")
    parser.add_argument("--yes", "-y", action="store_true", help="No pedir confirmación.")
    return parser


if __name__ == "__main__":
    asyncio.run(main(build_parser().parse_args()))
