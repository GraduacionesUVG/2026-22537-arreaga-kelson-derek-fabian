"""
Fija la tarifa de servicio (Q por pedido) de un restaurante, buscándolo por nombre.
Es lo mismo que PUT /api/admin/restaurant/service-fee desde el panel, para la base ya existente.

Uso:
    docker compose exec api python -m scripts.set_service_fee --restaurant "Golden Harvest" --fee 2.65
"""

import argparse
import asyncio
import re

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.models.restaurant import MAX_SERVICE_FEE


async def main(name: str, fee: float) -> None:
    if not 0 <= fee <= MAX_SERVICE_FEE:
        raise SystemExit(f"La tarifa debe estar entre 0 y {MAX_SERVICE_FEE}.")
    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]
    result = await db.restaurants.update_one(
        {"name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}},
        {"$set": {"service_fee": round(fee, 2)}},
    )
    client.close()
    if result.matched_count == 0:
        raise SystemExit(f"No existe un restaurante llamado '{name}'.")
    print(f"Tarifa de servicio de '{name}': Q{fee:.2f} por pedido.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--restaurant", required=True, help="Nombre del restaurante (sin distinguir mayúsculas).")
    parser.add_argument("--fee", required=True, type=float, help="Quetzales por pedido (0 = sin tarifa).")
    args = parser.parse_args()
    asyncio.run(main(args.restaurant, args.fee))
