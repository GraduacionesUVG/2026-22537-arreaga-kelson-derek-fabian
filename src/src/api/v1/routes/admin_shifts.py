"""Panel de administración: resumen de tickets por día y por turno de las estaciones KDS."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import AdminScope, get_admin_scope
from src.core.database import get_db
from src.core.timeutils import local_today
from src.schemas.shift import DaySummaryOut, ShiftSummaryOut
from src.services.shift_service import ShiftService

router = APIRouter()


@router.get("/shifts", response_model=DaySummaryOut, summary="Resumen de un día: KPIs, turnos y tickets")
async def day_summary(
    on_date: date | None = Query(default=None, alias="date", description="YYYY-MM-DD, default hoy"),
    kds_user_id: str | None = Query(default=None, description="Solo una estación"),
    restaurant_id: str | None = Query(default=None, description="Superadmin: id del restaurante"),
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Sin `kds_user_id`, los KPIs son del restaurante (cada solicitud cuenta una vez, con su
    resultado final). Con `kds_user_id`, los KPIs y tickets se ven desde esa estación."""
    day = on_date or local_today()
    rid = scope.resolve_restaurant(restaurant_id)
    return await ShiftService(db).day_summary(rid, day.isoformat(), kds_user_id)


@router.get("/shifts/{shift_id}", response_model=ShiftSummaryOut, summary="Resumen de un turno")
async def shift_summary(
    shift_id: str,
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    service = ShiftService(db)
    shift = await service.get_shift(shift_id, restaurant_id=None if scope.is_superadmin else scope.restaurant_id)
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turno no encontrado.")
    return await service.summary(shift)
