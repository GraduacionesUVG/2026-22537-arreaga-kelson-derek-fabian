"""Repositorio de `conversation_metrics`, ver src/models/conversation_metric.py."""

from src.core.timeutils import utc_now
from src.models.conversation_metric import COLLECTION_NAME
from src.repositories.base_repo import BaseRepository


class ConversationMetricRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def upsert_by_order(self, order_id: str, doc: dict) -> None:
        """Idempotente: si el agente reintenta el envío no se duplica el pedido."""
        await self.collection.update_one(
            {"order_id": order_id},
            {"$set": doc, "$setOnInsert": {"created_at": utc_now()}},
            upsert=True,
        )

    async def in_range(self, from_date: str | None, to_date: str | None) -> list[dict]:
        query: dict = {}
        if from_date or to_date:
            query["local_date"] = {}
            if from_date:
                query["local_date"]["$gte"] = from_date
            if to_date:
                query["local_date"]["$lte"] = to_date
        return await self.find_all(query)
