"""
Crea (o actualiza) un estudiante ya verificado directamente en Mongo, para
poder probar `/api/orders` y el resto de la API sin pasar por el flujo real
de verificación por correo — útil mientras no se tiene acceso a la API de
WhatsApp/Meta para probar de punta a punta.

Uso (correr como módulo: `python scripts/x.py` no resuelve `src` en Docker,
donde el proyecto no queda instalado en el venv — ver Dockerfile):
    uv run python -m scripts.seed_test_student
"""

import asyncio
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.repositories.user_repo import UserRepository

DEFAULTS = {
    "whatsapp_number": "+50255551234",
    "full_name": "Estudiante de Prueba",
    "email": "estudiante.prueba@uvg.edu.gt",
    "student_id": "0000-24-00000",
}


def _prompt(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


async def main() -> None:
    print("=== Crear estudiante de prueba (ya verificado) ===")
    whatsapp_number = _prompt("Número de WhatsApp", DEFAULTS["whatsapp_number"])
    full_name = _prompt("Nombre completo", DEFAULTS["full_name"])
    email = _prompt("Correo", DEFAULTS["email"])
    student_id = _prompt("Carné", DEFAULTS["student_id"])

    client = AsyncIOMotorClient(settings.mongo.uri)
    db = client[settings.mongo.db_name]
    now = datetime.now(timezone.utc)

    repo = UserRepository(db)
    data = {
        "whatsapp_number": whatsapp_number,  # el repositorio lo guarda cifrado
        "full_name": full_name,
        "email": email,
        "student_id": student_id,
        "email_verified": True,
        "otp_code_hash": None,
        "otp_expires_at": None,
        "otp_attempts": 0,
        "last_interaction_at": now,
    }
    existing = await repo.find_by_whatsapp_number(whatsapp_number)
    if existing:
        await repo.update_one(existing["id"], data)
    else:
        await repo.insert_one({**data, "created_at": now})

    print(f"\nEstudiante '{full_name}' ({whatsapp_number}) listo para probar /api/orders.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
