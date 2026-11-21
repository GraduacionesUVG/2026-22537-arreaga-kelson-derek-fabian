"""Repositorio de la memoria del agente (`user_memory`), ver src/models/user_memory.py."""

from src.core.timeutils import utc_now
from src.models.user_memory import COLLECTION_NAME, CURRENT_SCHEMA_VERSION
from src.repositories.base_repo import BaseRepository


class UserMemoryRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_by_student_id(self, student_id: str) -> dict | None:
        return await self.find_one({"student_id": student_id})

    async def upsert_sections(self, student_id: str, sections: dict) -> dict:
        """`$set` solo de las secciones recibidas (no pisa las demás); crea el
        documento si no existe. Siempre refresca `updated_at` (índice TTL)."""
        now = utc_now()
        await self.collection.update_one(
            {"student_id": student_id},
            {
                "$set": {**sections, "schema_version": CURRENT_SCHEMA_VERSION, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return await self.find_by_student_id(student_id)

    async def delete_by_student_id(self, student_id: str) -> bool:
        result = await self.collection.delete_one({"student_id": student_id})
        return result.deleted_count == 1
