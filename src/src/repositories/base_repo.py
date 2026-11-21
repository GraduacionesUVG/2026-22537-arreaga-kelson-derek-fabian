"""
Repositorio genérico reutilizable para colecciones de MongoDB.

Los repositorios concretos (ej. OrderRepository) heredan de esta clase para
no reescribir las operaciones CRUD básicas. Esto es el equivalente, para
Mongo + Motor, del `session` que la plantilla original pasaba a
`ItemRepository` para SQL.

Convención: todos los métodos son async porque Motor es un driver
asíncrono; los servicios y rutas que los llamen también deben ser async.
"""

from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ReturnDocument


def to_object_id(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except InvalidId as exc:
        raise ValueError(f"'{id_str}' no es un ObjectId válido de MongoDB.") from exc


def serialize_document(doc: dict | None) -> dict | None:
    """Convierte `_id` (ObjectId) a `id` (str) para que el documento sea serializable por Pydantic/JSON."""
    if doc is None:
        return None
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


class BaseRepository:
    """CRUD genérico sobre una colección de Mongo. Extiéndela por dominio (ver order_repo.py)."""

    collection_name: str = ""

    def __init__(self, db: AsyncIOMotorDatabase):
        if not self.collection_name:
            raise ValueError("Las subclases deben definir `collection_name`.")
        self.collection: AsyncIOMotorCollection = db[self.collection_name]

    async def find_by_id(self, id_str: str) -> dict | None:
        doc = await self.collection.find_one({"_id": to_object_id(id_str)})
        return serialize_document(doc)

    async def find_many(self, filter_: dict[str, Any] | None = None, limit: int = 100) -> list[dict]:
        cursor = self.collection.find(filter_ or {}).limit(limit)
        return [serialize_document(doc) async for doc in cursor]

    async def find_all(self, filter_: dict[str, Any] | None = None, sort: list | None = None) -> list[dict]:
        """Sin límite -- para colecciones acotadas por restaurante/día."""
        cursor = self.collection.find(filter_ or {})
        if sort:
            cursor = cursor.sort(sort)
        return [serialize_document(doc) async for doc in cursor]

    async def find_one(self, filter_: dict[str, Any]) -> dict | None:
        return serialize_document(await self.collection.find_one(filter_))

    async def update_where(self, id_str: str, conditions: dict[str, Any], data: dict) -> dict | None:
        """Actualización condicional atómica: solo aplica si el documento sigue
        cumpliendo `conditions`. Devuelve el documento actualizado o `None`."""
        doc = await self.collection.find_one_and_update(
            {"_id": to_object_id(id_str), **conditions},
            {"$set": data},
            return_document=ReturnDocument.AFTER,
        )
        return serialize_document(doc)

    async def insert_one(self, data: dict) -> dict:
        result = await self.collection.insert_one(data)
        created = await self.collection.find_one({"_id": result.inserted_id})
        return serialize_document(created)

    async def update_one(self, id_str: str, data: dict) -> dict | None:
        await self.collection.update_one({"_id": to_object_id(id_str)}, {"$set": data})
        return await self.find_by_id(id_str)

    async def delete_one(self, id_str: str) -> bool:
        result = await self.collection.delete_one({"_id": to_object_id(id_str)})
        return result.deleted_count == 1
