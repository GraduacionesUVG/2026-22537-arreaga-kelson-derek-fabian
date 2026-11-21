"""Secuencias atómicas (`counters`): request_number, order_number, event_seq."""

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

COLLECTION_NAME = "counters"

# Valor inicial de cada secuencia (el primer número entregado es inicial + 1).
SEQUENCE_START = {"request_number": 1000, "order_number": 2000, "event_seq": 0}


class CounterRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.collection = db[COLLECTION_NAME]

    async def next(self, name: str) -> int:
        start = SEQUENCE_START.get(name, 0)
        doc = await self.collection.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}, "$setOnInsert": {"start": start}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return start + doc["seq"]
