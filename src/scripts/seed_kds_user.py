"""
Crea un usuario de KDS (pantalla de cocina) de prueba directamente en
Mongo, con usuario + PIN numérico (ver src/models/kds_user.py) -- distinto
de `admin_users` (correo + contraseña, ver scripts/seed_admin_user.py).

Login: `POST /auth/kds/login` con `username` + `pin`. El front guarda
`username`/`kds_user_id`/`restaurant_name` de la respuesta localmente para
pedir solo el PIN en las siguientes visitas.

Uso (correr como módulo: `python scripts/x.py` no resuelve `src` en Docker,
donde el proyecto no queda instalado en el venv — ver Dockerfile):
    uv run python -m scripts.seed_kds_user

Dentro de Docker:
    docker compose exec api python -m scripts.seed_kds_user
"""

import asyncio
import getpass
import re
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.core.security import hash_pin
from src.models.kds_user import COLLECTION_NAME

PIN_PATTERN = re.compile(r"^\d{4,8}$")


async def main() -> None:
    print("=== Crear usuario de KDS (usuario + PIN) ===")
    username = input("Usuario (esto es lo que se usa para iniciar sesión, único): ").strip()
    restaurant_name = input("Nombre de restaurante: ").strip()
    restaurant_id = input("restaurant_id (Mongo _id, opcional, Enter para omitir): ").strip() or None
    pin = getpass.getpass("PIN temporal (4 a 8 dígitos, solo números): ")

    if not username:
        raise SystemExit("El usuario no puede estar vacío.")
    if not restaurant_name:
        raise SystemExit("El nombre de restaurante no puede estar vacío.")
    if not PIN_PATTERN.match(pin):
        raise SystemExit("El PIN debe tener entre 4 y 8 dígitos, solo números.")

    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]

    if await db[COLLECTION_NAME].find_one({"username": username}):
        client.close()
        raise SystemExit(f"Ya existe un usuario de KDS con el usuario '{username}'.")

    await db[COLLECTION_NAME].insert_one(
        {
            "username": username,
            "pin_hash": hash_pin(pin),
            "restaurant_name": restaurant_name,
            "restaurant_id": restaurant_id,
            "menu_filter": {"mode": "all", "item_ids": [], "categories": []},
            "priority": 1,
            "active": True,
            "token_version": 0,
            "created_at": datetime.now(timezone.utc),
            "last_login_at": None,
        }
    )

    print(f"\nUsuario de KDS '{username}' ({restaurant_name}) creado.")
    print("Login: POST /auth/kds/login con este usuario y el PIN temporal.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
