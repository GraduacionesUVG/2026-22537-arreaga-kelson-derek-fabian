"""Repositorio del catálogo (`menu_items`). Los borrados son lógicos (`deleted = true`)."""

from src.models.menu import CATEGORIES_COLLECTION_NAME, COLLECTION_NAME
from src.repositories.base_repo import BaseRepository

NOT_DELETED = {"deleted": {"$ne": True}}


class MenuRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_for_restaurant(self, restaurant_id: str, extra: dict | None = None) -> list[dict]:
        return await self.find_all(
            {"restaurant_id": restaurant_id, **NOT_DELETED, **(extra or {})},
            sort=[("category", 1), ("serve_date", 1), ("option_number", 1), ("name", 1)],
        )

    async def find_by_code(self, restaurant_id: str, code: str) -> dict | None:
        return await self.find_one({"restaurant_id": restaurant_id, "code": code, **NOT_DELETED})

    async def find_calendar(self, restaurant_id: str, from_date: str | None, to_date: str | None) -> list[dict]:
        date_filter: dict = {"$ne": None}
        if from_date:
            date_filter["$gte"] = from_date
        if to_date:
            date_filter["$lte"] = to_date
        return await self.find_for_restaurant(restaurant_id, {"schedule_type": "date", "serve_date": date_filter})

    async def soft_delete_many(self, ids: list[str], now) -> None:
        from src.repositories.base_repo import to_object_id

        if not ids:
            return
        await self.collection.update_many(
            {"_id": {"$in": [to_object_id(i) for i in ids]}},
            {"$set": {"deleted": True, "deleted_at": now, "updated_at": now}},
        )

    async def set_category_many(self, restaurant_id: str, ids: list[str], category: str, now) -> None:
        """Renombra la categoría de los items `ids` (renombrado en cascada de una categoría)."""
        from src.repositories.base_repo import to_object_id

        if not ids:
            return
        await self.collection.update_many(
            {"_id": {"$in": [to_object_id(i) for i in ids]}, "restaurant_id": restaurant_id},
            {"$set": {"category": category, "updated_at": now}},
        )

    async def pause_many(self, restaurant_id: str, ids: list[str], day: str, now) -> None:
        """Desactiva los items solo por el día `day` (YYYY-MM-DD, hora de Guatemala)."""
        from src.repositories.base_repo import to_object_id

        if not ids:
            return
        await self.collection.update_many(
            {"_id": {"$in": [to_object_id(i) for i in ids]}, "restaurant_id": restaurant_id},
            {"$set": {"paused_on": day, "updated_at": now}},
        )


class MenuCategoryRepository(BaseRepository):
    """Categorías creadas desde el panel (`menu_categories`). `name_key` = nombre en minúsculas."""

    collection_name = CATEGORIES_COLLECTION_NAME

    async def find_for_restaurant(self, restaurant_id: str) -> list[dict]:
        return await self.find_all({"restaurant_id": restaurant_id}, sort=[("name_key", 1)])

    async def find_by_key(self, restaurant_id: str, name_key: str) -> dict | None:
        return await self.find_one({"restaurant_id": restaurant_id, "name_key": name_key})

    async def upsert_by_key(self, restaurant_id: str, name_key: str, data: dict, now) -> None:
        """Crea o actualiza la categoría `name_key` (las implícitas, que solo existen en productos, se crean acá)."""
        await self.collection.update_one(
            {"restaurant_id": restaurant_id, "name_key": name_key},
            {"$set": data, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    async def delete_by_key(self, restaurant_id: str, name_key: str) -> int:
        result = await self.collection.delete_many({"restaurant_id": restaurant_id, "name_key": name_key})
        return result.deleted_count
