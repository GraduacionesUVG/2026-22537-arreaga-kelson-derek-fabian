"""Repositorio de usuarios de administración (`admin_users`)."""

from src.models.admin_user import COLLECTION_NAME
from src.repositories.base_repo import BaseRepository


class AdminUserRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_by_email(self, email: str) -> dict | None:
        return await self.find_one({"email": email.strip().lower()}) or await self.find_one({"email": email})

    async def find_by_username(self, username: str) -> list[dict]:
        return await self.find_all({"username": username})

    async def list_admins(self, restaurant_id: str | None = None, role: str | None = None) -> list[dict]:
        filter_: dict = {}
        if restaurant_id:
            filter_["restaurant_id"] = restaurant_id
        if role:
            filter_["role"] = role
        return await self.find_all(filter_, sort=[("restaurant_name", 1), ("email", 1)])
