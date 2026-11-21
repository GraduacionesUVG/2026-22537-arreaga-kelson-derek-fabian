"""
Backend de ENSAYO para la demo: levanta la API real (main.py) sobre una base de
datos EN MEMORIA (mongomock-motor) sembrada con un "Local de Prueba" que imita
al de producción. Sirve para ensayar la grabación sin crear pedidos en prod.

Nunca se conecta a MongoDB: `mongo.connect()` se reemplaza antes de arrancar,
así que aunque el `.env` apunte a producción, nada sale de este proceso.

    uv run --group dev python demo/dev_backend.py            # API en http://localhost:8000
    uv run python demo/demo_server.py --target http://localhost:8000   # (otra terminal)

Credenciales del ensayo (todas en memoria, las mismas que crea scripts/seed_demo_restaurant.py):
    Admin  admin@localdeprueba.com / Demo2026!     KDS  lp-cocina / PIN 2468
    Dev/TI ops@qorder.demo / OpsDemo2026!          INTERNAL_API_KEY = ensayo-demo
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Variables ANTES de importar la app (pydantic-settings las lee al importar).
os.environ.update(
    {
        "ENVIRONMENT": "DEVELOPMENT",
        "MONGO_URI": "mongodb://ensayo-en-memoria.invalid:27017",
        "MONGO_DB_NAME": "ensayo_demo",
        "INTERNAL_API_KEY": os.getenv("DEMO_INTERNAL_API_KEY", "ensayo-demo"),
        "KAFKA_ENABLED": "false",
        "NOTIFY_INLINE_WITHOUT_KAFKA": "false",
        "RESEND_API_KEY": "",
        "SIMULATED_PAYMENTS_ENABLED": "true",
        "LAST_ORDER_MINUTES_BEFORE_CLOSE": "0",
        "BACKEND_BASE_URL": "http://localhost:8000",
        "WHATSAPP_TOKEN": "",
    }
)

import asyncio  # noqa: E402

import uvicorn  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from scripts.seed_demo_restaurant import ALWAYS_OPEN, build_parser, seed_demo  # noqa: E402
from src.core import database  # noqa: E402

OPS_EMAIL = "ops@qorder.demo"
OPS_PASSWORD = "OpsDemo2026!"

_client = AsyncMongoMockClient(tz_aware=True)
_db = _client["ensayo_demo"]


def _fake_connect() -> None:
    database.mongo.client = _client
    database.mongo.db = _db
    print("[mongo] ENSAYO: base de datos en memoria (no se conecta a ningún servidor).")


database.mongo.connect = _fake_connect
database.mongo.close = lambda: None


async def seed() -> None:
    """Mismo restaurante, admin, KDS y menú que scripts/seed_demo_restaurant.py, más un usuario Dev/TI."""
    args = build_parser().parse_args(["--ops-email", OPS_EMAIL, "--ops-password", OPS_PASSWORD, "--yes"])
    await _db.restaurants.insert_one({"name": "Golden Harvest", "location": "Edificio A", "active": True,
                                      "opening_hours": ALWAYS_OPEN})
    await seed_demo(_db, args)


def main() -> None:
    from main import app

    # mongomock-motor no ata la base a un event loop: se puede sembrar antes de arrancar uvicorn.
    asyncio.run(seed())
    print("[ensayo] Todo vive en memoria. INTERNAL_API_KEY=" + os.environ["INTERNAL_API_KEY"])

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("DEV_BACKEND_PORT", "8000")), log_level="warning")


if __name__ == "__main__":
    main()
