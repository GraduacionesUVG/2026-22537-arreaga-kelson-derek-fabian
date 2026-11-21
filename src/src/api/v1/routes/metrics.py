"""KPIs y datos para gráficas (JSON). Sirve al panel web y a herramientas de BI."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import AdminScope, get_admin_scope, get_conversation_metric_service, require_superadmin
from src.core.database import get_db
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.order_repo import OrderRepository
from src.services.conversation_metric_service import ConversationMetricService
from src.services.kpi_service import KpiService

router = APIRouter()


@router.get("/kpis", summary="KPIs por restaurante, estación KDS o generales")
async def kpis(
    scope_: Literal["restaurant", "kds", "global"] = Query(default="restaurant", alias="scope"),
    restaurant_id: str | None = None,
    kds_user_id: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "'from' no puede ser mayor que 'to'.")
    rid = None
    if scope_ == "global":
        if not scope.is_superadmin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el equipo Dev/TI puede ver KPIs generales.")
    elif scope_ == "kds":
        if not kds_user_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Indica kds_user_id.")
        station = await KdsUserRepository(db).find_by_id(kds_user_id)
        if station is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario KDS no encontrado.")
        rid = scope.resolve_restaurant(station.get("restaurant_id"))
    else:
        rid = scope.resolve_restaurant(restaurant_id)
    return await KpiService(OrderRepository(db)).kpis(
        scope_, rid, kds_user_id, from_date.isoformat() if from_date else None, to_date.isoformat() if to_date else None
    )


@router.get("/dashboard", summary="Alias de /kpis con scope=restaurant")
async def dashboard_metrics(
    restaurant_id: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    rid = scope.resolve_restaurant(restaurant_id)
    return await KpiService(OrderRepository(db)).kpis(
        "restaurant", rid, None, from_date.isoformat() if from_date else None, to_date.isoformat() if to_date else None
    )


@router.get("/conversation-times", summary="Tiempos de conversación del agente (objetivo < 45 s)")
async def conversation_times(
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    _: AdminScope = Depends(require_superadmin),
    service: ConversationMetricService = Depends(get_conversation_metric_service),
):
    """Percentiles y % de pedidos bajo 45 s. Solo Dev/TI: es evidencia del proyecto, no del restaurante."""
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "'from' no puede ser mayor que 'to'.")
    return await service.summary(from_date.isoformat() if from_date else None, to_date.isoformat() if to_date else None)
