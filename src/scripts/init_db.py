"""
Crea (si no existen) los índices de todas las colecciones del proyecto en
la base configurada por MONGO_URI/MONGO_DB_NAME (.env). Mongo crea las
colecciones solas la primera vez que se les crea un índice o se inserta un
documento — correr este script una vez es suficiente para dejar la base
lista, tanto en Atlas como local.

Es idempotente: se puede correr varias veces sin duplicar índices ni fallar
si ya existen.

Uso:
    uv run python -m scripts.init_db

Para dejar la base 100% limpia antes de sembrar , agregar `--reset`: borra las colecciones del
proyecto ANTES de crear los índices. Pide confirmación explícita porque es
destructivo e irreversible.

    uv run python -m scripts.init_db --reset
"""

import argparse
import asyncio

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING

from src.configuration import settings
from src.core.database import redact_uri
from src.repositories.user_repo import ensure_user_phone_indexes

# Únicas colecciones que este proyecto conoce/gestiona -- --reset solo toca estas
PROJECT_COLLECTIONS = [
    "users",
    "restaurants",
    "menu_items",
    "orders",
    "admin_users",
    "kds_users",
    "kds_shifts",
    "order_events",
    "counters",
    "password_reset_tokens",
    "user_memory",
    "conversation_metrics",
]


async def reset(db) -> None:
    for name in PROJECT_COLLECTIONS:
        await db[name].drop()
    print(f"Colecciones borradas: {PROJECT_COLLECTIONS}")


async def create_indexes(db) -> None:
    # users — ver src/models/user.py. Un mismo documento cubre "registrado
    # pero sin verificar" (email_verified=False, con otp_code_hash/otp_expires_at)
    # y "verificado" -- ya no hay una colección `pending_registrations` aparte.
    # El número de WhatsApp va cifrado: el índice único es sobre su hash (src/core/pii.py).
    await ensure_user_phone_indexes(db)
    await db.users.create_index([("email", ASCENDING)], unique=True, name="uniq_email")
    await db.users.create_index([("student_id", ASCENDING)], unique=True, name="uniq_student_id")
    await db.users.create_index(
        [("last_interaction_at", ASCENDING)],
        expireAfterSeconds=settings.auth.inactive_user_ttl_days * 86400,
        name="ttl_inactivity",
    )

    # restaurants — ver src/models/restaurant.py
    await db.restaurants.create_index([("active", ASCENDING)], name="active")

    # menu_items — ver src/models/menu.py
    await db.menu_items.create_index(
        [("restaurant_id", ASCENDING), ("category", ASCENDING)], name="restaurant_category"
    )
    await db.menu_items.create_index(
        [("restaurant_id", ASCENDING), ("available", ASCENDING)], name="restaurant_available"
    )
    await db.menu_items.create_index(
        [("restaurant_id", ASCENDING), ("name", ASCENDING), ("day_of_week", ASCENDING)],
        name="restaurant_name_day",
    )
    await db.menu_items.create_index([("restaurant_id", ASCENDING), ("code", ASCENDING)], name="restaurant_code")
    await db.menu_items.create_index(
        [("restaurant_id", ASCENDING), ("schedule_type", ASCENDING), ("serve_date", ASCENDING)],
        name="restaurant_calendar",
    )

    # menu_categories — categorías creadas desde el panel (ver src/models/menu.py)
    await db.menu_categories.create_index(
        [("restaurant_id", ASCENDING), ("name_key", ASCENDING)], unique=True, name="uniq_restaurant_category"
    )

    # orders — ver src/models/order.py (pipeline_status/payment_status, no un solo "status")
    await db.orders.create_index([("pipeline_status", ASCENDING)], name="pipeline_status")
    await db.orders.create_index(
        [("restaurant_id", ASCENDING), ("pipeline_status", ASCENDING), ("payment_status", ASCENDING)],
        name="restaurant_pipeline_payment",
    )
    await db.orders.create_index([("student_id", ASCENDING), ("created_at", DESCENDING)], name="student_history")
    await db.orders.create_index([("restaurant_id", ASCENDING), ("local_date", ASCENDING)], name="restaurant_day")
    await db.orders.create_index([("request_number", ASCENDING)], name="request_number")
    await db.orders.create_index([("order_number", ASCENDING)], name="order_number")
    await db.orders.create_index([("offered_kds_id", ASCENDING), ("pipeline_status", ASCENDING)], name="offered_station")
    await db.orders.create_index([("assigned_kds_id", ASCENDING), ("pipeline_status", ASCENDING)], name="assigned_station")
    await db.orders.create_index(
        [("pipeline_status", ASCENDING), ("payment_status", ASCENDING), ("payment_expires_at", ASCENDING)],
        name="payment_expiry",
    )

    # order_events — ver src/models/order_event.py
    await db.order_events.create_index([("restaurant_id", ASCENDING), ("local_date", ASCENDING), ("seq", ASCENDING)], name="restaurant_day_seq")
    await db.order_events.create_index([("order_id", ASCENDING), ("seq", ASCENDING)], name="order_seq")
    await db.order_events.create_index([("kds_user_ids", ASCENDING), ("local_date", ASCENDING)], name="station_day")
    await db.order_events.create_index([("local_date", ASCENDING), ("seq", ASCENDING)], name="day_seq")

    # password_reset_tokens — token de un solo uso; Mongo borra los vencidos a las 24 h.
    await db.password_reset_tokens.create_index([("token_hash", ASCENDING)], unique=True, name="uniq_token_hash")
    await db.password_reset_tokens.create_index([("expires_at", ASCENDING)], expireAfterSeconds=86400, name="ttl_expired")

    # admin_users — ver src/models/admin_user.py. El login es por email
    # (unique); `username` es solo un label para mostrar y puede repetirse
    # (ej. varios "Admin"), por eso no lleva índice único.
    await db.admin_users.create_index([("email", ASCENDING)], unique=True, name="uniq_email")

    # kds_users — ver src/models/kds_user.py. Acá `username` SÍ es único: es
    # lo que se usa para el login (usuario + PIN), a diferencia de admin_users.
    await db.kds_users.create_index([("username", ASCENDING)], unique=True, name="uniq_username")
    await db.kds_users.create_index([("restaurant_id", ASCENDING), ("priority", ASCENDING)], name="restaurant_priority")

    # kds_shifts — ver src/models/kds_shift.py. Un solo turno abierto por estación y
    # numeración única por estación y día.
    await db.kds_shifts.create_index(
        [("kds_user_id", ASCENDING), ("local_date", ASCENDING), ("shift_no", ASCENDING)],
        unique=True,
        name="uniq_station_day_shift_no",
    )
    await db.kds_shifts.create_index(
        [("kds_user_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"status": "open"},
        name="uniq_open_shift_per_station",
    )
    await db.kds_shifts.create_index(
        [("restaurant_id", ASCENDING), ("started_at", ASCENDING)], name="restaurant_started"
    )

    # user_memory — ver src/models/user_memory.py. Un documento por estudiante;
    # se borra solo tras INACTIVE_USER_TTL_DAYS sin actividad, igual que `users`.
    await db.user_memory.create_index([("student_id", ASCENDING)], unique=True, name="uniq_student_id")
    await db.user_memory.create_index(
        [("updated_at", ASCENDING)],
        expireAfterSeconds=settings.auth.inactive_user_ttl_days * 86400,
        name="ttl_inactivity",
    )

    # conversation_metrics — ver src/models/conversation_metric.py. Un documento por pedido.
    await db.conversation_metrics.create_index([("order_id", ASCENDING)], unique=True, name="uniq_order_id")
    await db.conversation_metrics.create_index([("local_date", ASCENDING)], name="by_day")


async def main(do_reset: bool) -> None:
    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]

    if do_reset:
        print(f"\nEsto BORRA todas las colecciones del proyecto en '{settings.mongo.db_name}' ({redact_uri(settings.mongo.uri)}).")
        print("No se puede deshacer.")
        confirm = input("Escribe BORRAR (en mayúsculas) para continuar: ")
        if confirm != "BORRAR":
            client.close()
            raise SystemExit("Cancelado, no se tocó nada.")
        await reset(db)

    await create_indexes(db)

    collections = await db.list_collection_names()
    print(f"\nÍndices creados/verificados en '{settings.mongo.db_name}' ({redact_uri(settings.mongo.uri)}).")
    print(f"Colecciones existentes: {sorted(collections)}")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="Borra las colecciones del proyecto antes de indexar.")
    args = parser.parse_args()
    asyncio.run(main(args.reset))
