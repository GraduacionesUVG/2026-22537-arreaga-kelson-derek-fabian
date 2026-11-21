"""Repositorio de restaurantes (`restaurants`)."""

from src.models.restaurant import COLLECTION_NAME
from src.repositories.base_repo import BaseRepository


class RestaurantRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_by_name(self, name: str) -> dict | None:
        return await self.find_one({"name": name})

    async def list_all(self) -> list[dict]:
        return await self.find_all({}, sort=[("name", 1)])
