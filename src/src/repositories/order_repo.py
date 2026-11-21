"""Repositorio de pedidos (orders)."""

from datetime import datetime, timezone

from src.models.order import COLLECTION_NAME, OrderPaymentStatus, OrderPipelineStatus
from src.repositories.base_repo import BaseRepository, serialize_document

# Pedidos que todavía están "vivos" en la cola visible del KDS.
ACTIVE_PIPELINE_STATUSES = [
    OrderPipelineStatus.PENDING_ACCEPTANCE,
    OrderPipelineStatus.ACCEPTED,
    OrderPipelineStatus.IN_PREPARATION,
    OrderPipelineStatus.READY_FOR_PICKUP,
]

# Pedidos ya pagados que ocupan tiempo real de cocina (backlog del ETC).
KITCHEN_QUEUE_PIPELINE_STATUSES = [OrderPipelineStatus.ACCEPTED, OrderPipelineStatus.IN_PREPARATION]

_ACTIVE = [s.value for s in ACTIVE_PIPELINE_STATUSES]


class OrderRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_active(
        self, restaurant_id: str | None = None, kds_user_id: str | None = None
    ) -> list[dict]:
        """Cola visible del KDS. Con `kds_user_id`: solo lo ofrecido o asignado a
        esa estación, más lo que no tiene estación (restaurantes sin estaciones)."""
        filter_: dict = {"pipeline_status": {"$in": _ACTIVE}}
        if restaurant_id:
            filter_["restaurant_id"] = restaurant_id
        if kds_user_id:
            filter_["$or"] = [
                {"offered_kds_id": kds_user_id, "pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value},
                {"assigned_kds_id": kds_user_id},
                {"offered_kds_id": None, "assigned_kds_id": None},
            ]
        return await self.find_all(filter_, sort=[("created_at", 1)])

    async def find_kitchen_queue(self, restaurant_id: str, kds_user_id: str | None = None) -> list[dict]:
        filter_ = {
            "restaurant_id": restaurant_id,
            "pipeline_status": {"$in": [s.value for s in KITCHEN_QUEUE_PIPELINE_STATUSES]},
            "payment_status": OrderPaymentStatus.PAID.value,
        }
        if kds_user_id:
            filter_["assigned_kds_id"] = kds_user_id
        return await self.find_all(filter_)

    async def count_active_for_station(self, kds_user_id: str) -> int:
        return await self.collection.count_documents(
            {
                "pipeline_status": {"$in": _ACTIVE},
                "$or": [{"offered_kds_id": kds_user_id}, {"assigned_kds_id": kds_user_id}],
            }
        )

    async def count_for_station_on(self, kds_user_id: str, local_date: str) -> int:
        return await self.collection.count_documents(
            {"local_date": local_date, "$or": [{"offered_kds_id": kds_user_id}, {"assigned_kds_id": kds_user_id}]}
        )

    async def count_pending_for_station(self, kds_user_id: str) -> int:
        """Solicitudes ofrecidas a la estación que todavía esperan aceptar/rechazar."""
        return await self.collection.count_documents(
            {"offered_kds_id": kds_user_id, "pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value}
        )

    async def count_in_progress_for_station(self, kds_user_id: str) -> int:
        """Órdenes aceptadas por la estación que siguen en curso (esperando pago, en cola, en cocina o listas)."""
        return await self.collection.count_documents(
            {
                "assigned_kds_id": kds_user_id,
                "pipeline_status": {"$in": [s for s in _ACTIVE if s != OrderPipelineStatus.PENDING_ACCEPTANCE.value]},
            }
        )

    async def find_for_station_window(
        self, restaurant_id: str, kds_user_id: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Solicitudes creadas en `[start, end)` que pasaron por la estación (ofrecidas, rechazadas o asignadas)."""
        return await self.find_all(
            {
                "restaurant_id": restaurant_id,
                "created_at": {"$gte": start, "$lt": end},
                "$or": [
                    {"assigned_kds_id": kds_user_id},
                    {"offered_kds_id": kds_user_id},
                    {"declined_kds_ids": kds_user_id},
                ],
            },
            sort=[("created_at", 1)],
        )

    async def find_unanswered_requests(self, created_before: datetime) -> list[dict]:
        """Solicitudes que siguen esperando al restaurante y se crearon antes de `created_before`."""
        return await self.find_all(
            {
                "pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value,
                "created_at": {"$lt": created_before},
            }
        )

    async def find_expired_unpaid(self, now: datetime) -> list[dict]:
        return await self.find_all(
            {
                "pipeline_status": OrderPipelineStatus.ACCEPTED.value,
                "payment_status": OrderPaymentStatus.PENDING.value,
                "payment_expires_at": {"$ne": None, "$lt": now},
            }
        )

    async def find_for_day(
        self,
        local_date: str,
        restaurant_id: str | None = None,
        kds_user_id: str | None = None,
        pipeline_status: str | None = None,
    ) -> list[dict]:
        filter_: dict = {"local_date": local_date}
        if restaurant_id:
            filter_["restaurant_id"] = restaurant_id
        if kds_user_id:
            filter_["$or"] = [
                {"assigned_kds_id": kds_user_id},
                {"offered_kds_id": kds_user_id},
                {"declined_kds_ids": kds_user_id},
            ]
        if pipeline_status:
            filter_["pipeline_status"] = pipeline_status
        return await self.find_all(filter_, sort=[("created_at", 1)])

    async def find_in_range(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        restaurant_id: str | None = None,
        kds_user_id: str | None = None,
    ) -> list[dict]:
        filter_: dict = {}
        date_filter = {}
        if from_date:
            date_filter["$gte"] = from_date
        if to_date:
            date_filter["$lte"] = to_date
        if date_filter:
            filter_["local_date"] = date_filter
        if restaurant_id:
            filter_["restaurant_id"] = restaurant_id
        if kds_user_id:
            filter_["assigned_kds_id"] = kds_user_id
        return await self.find_all(filter_)

    async def sold_quantities_on(self, restaurant_id: str, local_date: str) -> dict[str, int]:
        """Unidades pedidas hoy por `menu_item_id` (excluye rechazados/cancelados)."""
        orders = await self.find_all(
            {
                "restaurant_id": restaurant_id,
                "local_date": local_date,
                "pipeline_status": {
                    "$nin": [OrderPipelineStatus.REJECTED.value, OrderPipelineStatus.CANCELLED.value]
                },
            }
        )
        sold: dict[str, int] = {}
        for order in orders:
            for item in order.get("items", []):
                key = item.get("menu_item_id")
                if key:
                    sold[key] = sold.get(key, 0) + int(item.get("quantity", 0))
        return sold

    async def find_user_history(self, student_id: str, limit: int = 50) -> list[dict]:
        """Pedidos que el estudiante de verdad pidió (pagados, no rechazados ni
        cancelados), del más nuevo al más viejo. Usa el índice `student_history`."""
        cursor = (
            self.collection.find(
                {
                    "student_id": student_id,
                    "payment_status": OrderPaymentStatus.PAID.value,
                    "pipeline_status": {
                        "$nin": [OrderPipelineStatus.REJECTED.value, OrderPipelineStatus.CANCELLED.value]
                    },
                }
            )
            .sort("created_at", -1)
            .limit(limit)
        )
        return [serialize_document(doc) async for doc in cursor]

    async def update_fields(self, id_str: str, data: dict) -> dict | None:
        return await self.update_one(id_str, {**data, "updated_at": datetime.now(timezone.utc)})

    async def update_pipeline_status(self, id_str: str, pipeline_status: OrderPipelineStatus, **extra) -> dict | None:
        return await self.update_fields(id_str, {"pipeline_status": pipeline_status.value, **extra})

    async def update_payment_status(self, id_str: str, payment_status: OrderPaymentStatus) -> dict | None:
        return await self.update_fields(id_str, {"payment_status": payment_status.value})
