"""Repositorio de usuarios KDS / estaciones (`kds_users`)."""

from src.models.kds_user import COLLECTION_NAME
from src.repositories.base_repo import BaseRepository


class KdsUserRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_by_username(self, username: str) -> dict | None:
        return await self.find_one({"username": username})

    async def find_for_restaurant(self, restaurant_id: str | None = None) -> list[dict]:
        filter_ = {"restaurant_id": restaurant_id} if restaurant_id else {}
        return await self.find_all(filter_, sort=[("restaurant_name", 1), ("priority", 1), ("username", 1)])

    async def set_menu_filter_categories(self, id_str: str, categories: list[str]) -> None:
        from src.repositories.base_repo import to_object_id

        await self.collection.update_one(
            {"_id": to_object_id(id_str)}, {"$set": {"menu_filter.categories": categories}}
        )

    async def increment_token_version(self, id_str: str, data: dict) -> dict | None:
        from src.repositories.base_repo import to_object_id

        await self.collection.update_one({"_id": to_object_id(id_str)}, {"$set": data, "$inc": {"token_version": 1}})
        return await self.find_by_id(id_str)
