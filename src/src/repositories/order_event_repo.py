"""Repositorio del registro de eventos de pedidos (`order_events`)."""

from src.models.order_event import COLLECTION_NAME
from src.repositories.base_repo import BaseRepository


class OrderEventRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_for_day(
        self, local_date: str, restaurant_id: str | None = None, kds_user_id: str | None = None
    ) -> list[dict]:
        filter_: dict = {"local_date": local_date}
        if restaurant_id:
            filter_["restaurant_id"] = restaurant_id
        if kds_user_id:
            filter_["kds_user_ids"] = kds_user_id
        return await self.find_all(filter_, sort=[("seq", 1)])

    async def find_for_order(self, order_id: str) -> list[dict]:
        return await self.find_all({"order_id": order_id}, sort=[("seq", 1)])

    async def find_station_rejections(self, order_ids: list[str], kds_user_id: str) -> list[dict]:
        """Rechazos hechos por una estación (`actor.id`) sobre esos pedidos, con su `data.reject_reason`."""
        if not order_ids:
            return []
        return await self.find_all(
            {
                "order_id": {"$in": order_ids},
                "event": {"$in": ["order_rejected_by_station", "order_rejected"]},
                "actor.id": kds_user_id,
            },
            sort=[("seq", 1)],
        )
