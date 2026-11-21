"""
Crea un usuario de administración/analítica de prueba directamente en
Mongo (sin pasar por un panel de administración, que todavía no existe).
Refleja el flujo real: el equipo crea las cuentas antes de entregarle el
sistema al restaurante (ver src/models/admin_user.py), con
`must_change_password=True`.

Esta colección (`admin_users`) es solo para
administración/analítica -- el login (`POST /auth/admin/login`) es por
**correo**, no por `username` (el correo es lo único garantizado único).

Uso (correr como módulo: `python scripts/x.py` no resuelve `src` en Docker,
donde el proyecto no queda instalado en el venv — ver Dockerfile):
    uv run python -m scripts.seed_admin_user

Dentro de Docker:
    docker compose exec api python -m scripts.seed_admin_user
"""

import asyncio
import getpass
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.core.security import hash_password
from src.models.admin_user import COLLECTION_NAME, AdminRole


async def main() -> None:
    print("=== Crear usuario de administración/analítica ===")
    email = input("Correo (esto es lo que se usa para iniciar sesión): ").strip().lower()
    username = input("Nombre a mostrar [Admin]: ").strip() or "Admin"
    role = input("Rol: 1 = admin de restaurante, 2 = superadmin Dev/TI [1]: ").strip() or "1"
    restaurant_id = None
    restaurant_name = None
    if role != "2":
        restaurant_id = input("restaurant_id (Mongo _id del restaurante): ").strip() or None
        if not restaurant_id:
            raise SystemExit("Un admin de restaurante necesita restaurant_id.")
    password = getpass.getpass("Contraseña temporal: ")

    if not email:
        raise SystemExit("El correo no puede estar vacío (es lo que se usa para el login).")
    if not password:
        raise SystemExit("La contraseña no puede estar vacía.")

    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]

    if await db[COLLECTION_NAME].find_one({"email": email}):
        client.close()
        raise SystemExit(f"Ya existe un usuario con el correo '{email}'.")

    if restaurant_id:
        from bson import ObjectId

        restaurant = await db.restaurants.find_one({"_id": ObjectId(restaurant_id)})
        if restaurant is None:
            client.close()
            raise SystemExit(f"No existe el restaurante '{restaurant_id}'.")
        restaurant_name = restaurant["name"]

    await db[COLLECTION_NAME].insert_one(
        {
            "email": email,
            "username": username,
            "password_hash": hash_password(password),
            "restaurant_name": restaurant_name,
            "restaurant_id": restaurant_id,
            "role": AdminRole.SUPERADMIN.value if role == "2" else AdminRole.RESTAURANT_ADMIN.value,
            "must_change_password": True,
            "created_at": datetime.now(timezone.utc),
            "last_login_at": None,
        }
    )

    print(f"\nUsuario '{username}' ({email}) creado.")
    print("Login: POST /auth/admin/login con este correo y la contraseña temporal.")
    print("Debe cambiarla en el primer login (POST /auth/admin/change-password).")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
