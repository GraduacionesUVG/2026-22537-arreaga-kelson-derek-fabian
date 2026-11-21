"""Tokens de restablecimiento de contraseña (`password_reset_tokens`). Solo se guarda el hash."""

from src.repositories.base_repo import BaseRepository

COLLECTION_NAME = "password_reset_tokens"


class PasswordResetRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_by_hash(self, token_hash: str) -> dict | None:
        return await self.find_one({"token_hash": token_hash})

    async def invalidate_for_admin(self, admin_id: str, now) -> None:
        await self.collection.update_many({"admin_id": admin_id, "used_at": None}, {"$set": {"used_at": now}})
