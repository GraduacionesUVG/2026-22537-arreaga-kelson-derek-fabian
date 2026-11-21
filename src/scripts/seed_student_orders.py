"""
Crea pedidos pagados y entregados de UN estudiante, para probar la memoria del
agente ("lo de siempre", "algo que no haya pedido") sin pasar por el flujo
completo de pago. Usa productos reales del menú del restaurante.

El primer producto elegido se repite en todos los pedidos (queda como "lo que
más pide"); el resto se reparte. Los pedidos quedan a las 13:00 (hora de
Guatemala) de los días anteriores.

Los documentos llevan `seed_tag = "student_orders_demo"`: cada corrida borra
los que creó la anterior para ese carné (`--clear` solo borra).

    uv run python -m scripts.seed_student_orders --student-id 0000-24-00000
    uv run python -m scripts.seed_student_orders --student-id 0000-24-00000 --count 8 --restaurant Gitane
    uv run python -m scripts.seed_student_orders --student-id 0000-24-00000 --clear

Requiere el restaurante y su menú (scripts/seed_menu_test_data.py). Con
ENVIRONMENT=PRODUCTION exige `--allow-production`.
"""

import argparse
import asyncio
import random
from datetime import datetime, time, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.core.timeutils import GUATEMALA_TZ, local_today

SEED_TAG = "student_orders_demo"


async def main(args) -> None:
    if settings.environment.upper() == "PRODUCTION" and not args.allow_production:
        raise SystemExit("ENVIRONMENT=PRODUCTION: agrega --allow-production si de verdad quieres poblar esta base.")

    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]

    deleted = await db.orders.delete_many({"seed_tag": SEED_TAG, "student_id": args.student_id})
    print(f"Pedidos de prueba anteriores borrados para {args.student_id}: {deleted.deleted_count}")
    if args.clear:
        client.close()
        return

    restaurant = await db.restaurants.find_one({"name": args.restaurant})
    if restaurant is None:
        raise SystemExit(f"No existe el restaurante '{args.restaurant}'. Corre primero scripts.seed_menu_test_data.")
    restaurant_id = str(restaurant["_id"])
    menu = await db.menu_items.find({"restaurant_id": restaurant_id, "available": True}).to_list(None)
    if len(menu) < 2:
        raise SystemExit(f"'{args.restaurant}' necesita al menos 2 productos disponibles.")

    rng = random.Random(args.seed)
    rng.shuffle(menu)
    favorite, others = menu[0], menu[1:4]

    orders = []
    for n in range(args.count):
        day = local_today() - timedelta(days=n + 1)
        created = datetime.combine(day, time(12, 45), tzinfo=GUATEMALA_TZ).astimezone(timezone.utc)
        picked = [favorite] + ([rng.choice(others)] if n % 2 == 0 else [])
        items = [
            {
                "product_name": item["name"],
                "quantity": 1,
                "modifiers": [],
                "notes": None,
                "unit_price": float(item["price"]),
                "prep_time_minutes": float(item.get("prep_time_minutes", 5)),
                "menu_item_id": str(item["_id"]),
                "category": item.get("category"),
                "line_total": float(item["price"]),
            }
            for item in picked
        ]
        orders.append(
            {
                "student_id": args.student_id,
                "restaurant_id": restaurant_id,
                "items": items,
                "pipeline_status": "delivered",
                "payment_status": "paid",
                "total_amount": sum(i["line_total"] for i in items),
                "requested_ready_time": created + timedelta(minutes=15),
                "paid_at": created,
                "delivered_at": created + timedelta(minutes=20),
                "local_date": day.isoformat(),
                "created_at": created,
                "updated_at": created,
                "seed_tag": SEED_TAG,
            }
        )
    await db.orders.insert_many(orders)

    print(f"{len(orders)} pedidos pagados creados en {args.restaurant} para {args.student_id}.")
    print(f"Lo que más pide: {favorite['name']}. Otros: {', '.join(o['name'] for o in others)}.")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--student-id", required=True, help="Carné del estudiante (el mismo de seed_test_student).")
    parser.add_argument("--restaurant", default="Golden Harvest")
    parser.add_argument("--count", type=int, default=5, help="Cuántos pedidos crear (default 5)")
    parser.add_argument("--seed", type=int, default=2026, help="Semilla aleatoria (mismos productos en cada corrida)")
    parser.add_argument("--clear", action="store_true", help="Solo borra los pedidos de prueba de ese carné")
    parser.add_argument("--allow-production", action="store_true")
    asyncio.run(main(parser.parse_args()))
