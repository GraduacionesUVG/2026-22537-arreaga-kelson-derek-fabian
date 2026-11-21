"""Repositorio de turnos de estaciones KDS (`kds_shifts`)."""

from datetime import datetime

from pymongo import DESCENDING

from src.models.kds_shift import COLLECTION_NAME, ShiftStatus
from src.repositories.base_repo import BaseRepository


class KdsShiftRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_open(self, kds_user_id: str) -> dict | None:
        return await self.find_one({"kds_user_id": kds_user_id, "status": ShiftStatus.OPEN.value})

    async def last_shift_no(self, kds_user_id: str, local_date: str) -> int:
        doc = await self.collection.find_one(
            {"kds_user_id": kds_user_id, "local_date": local_date}, sort=[("shift_no", DESCENDING)]
        )
        return int(doc["shift_no"]) if doc else 0

    async def find_for_station_day(self, kds_user_id: str, local_date: str) -> list[dict]:
        return await self.find_all(
            {"kds_user_id": kds_user_id, "local_date": local_date}, sort=[("started_at", 1)]
        )

    async def find_overlapping(
        self, restaurant_id: str, start: datetime, end: datetime, kds_user_id: str | None = None
    ) -> list[dict]:
        """Turnos que se cruzan con `[start, end)` (los abiertos cuentan hasta ahora)."""
        filter_: dict = {
            "restaurant_id": restaurant_id,
            "started_at": {"$lt": end},
            "$or": [{"ended_at": None}, {"ended_at": {"$gt": start}}],
        }
        if kds_user_id:
            filter_["kds_user_id"] = kds_user_id
        return await self.find_all(filter_, sort=[("started_at", 1)])
