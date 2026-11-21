"""Panel de administración: control de solicitudes y órdenes (eventos y línea de tiempo).

Para el día actual con el restaurante abierto (`live = true`) el panel se conecta
además a `WS /ws/admin`, que envía los eventos previos (`backfill`) y los nuevos en vivo.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import AdminScope, get_admin_scope
from src.core.database import get_db
from src.core.timeutils import local_today, utc_now
from src.models.restaurant import is_open_at
from src.repositories.order_event_repo import OrderEventRepository
from src.repositories.order_repo import OrderRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.admin import EventsDayOut, OrderEventOut
from src.schemas.order import OrderResponse

router = APIRouter()


async def _is_live(db, restaurant_id: str | None, day: date) -> bool:
    if day != local_today():
        return False
    if restaurant_id is None:
        return True
    restaurant = await RestaurantRepository(db).find_by_id(restaurant_id)
    return bool(restaurant) and is_open_at(restaurant, utc_now())


async def _order_in_scope(db, scope: AdminScope, order_id: str) -> dict:
    try:
        order = await OrderRepository(db).find_by_id(order_id)
    except ValueError:
        order = None
    if order is None or (not scope.is_superadmin and order.get("restaurant_id") != scope.restaurant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado.")
    return order


@router.get("/events", response_model=EventsDayOut, summary="Eventos de solicitudes y órdenes de un día")
async def list_events(
    on_date: date | None = Query(default=None, alias="date", description="YYYY-MM-DD, default hoy"),
    restaurant_id: str | None = Query(default=None, description="Superadmin: id o 'all'"),
    kds_user_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    day = on_date or local_today()
    rid = scope.resolve_restaurant(restaurant_id, allow_all=True)
    events = await OrderEventRepository(db).find_for_day(day.isoformat(), rid, kds_user_id)
    return {"date": day.isoformat(), "live": await _is_live(db, rid, day), "events": events}


@router.get("/orders", response_model=list[OrderResponse], summary="Solicitudes y órdenes de un día")
async def list_orders(
    on_date: date | None = Query(default=None, alias="date", description="YYYY-MM-DD, default hoy"),
    restaurant_id: str | None = Query(default=None, description="Superadmin: id o 'all'"),
    kds_user_id: str | None = None,
    pipeline_status: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    day = on_date or local_today()
    rid = scope.resolve_restaurant(restaurant_id, allow_all=True)
    return await OrderRepository(db).find_for_day(day.isoformat(), rid, kds_user_id, pipeline_status)


@router.get("/orders/lookup", response_model=OrderResponse, summary="Buscar por número de solicitud u orden")
async def lookup_order(
    request_number: int | None = None,
    order_number: int | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if request_number is None and order_number is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Indica request_number u order_number.")
    filter_ = {"request_number": request_number} if request_number is not None else {"order_number": order_number}
    if not scope.is_superadmin:
        filter_["restaurant_id"] = scope.restaurant_id
    order = await OrderRepository(db).find_one(filter_)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado.")
    return order


@router.get("/orders/{order_id}/timeline", summary="Proceso completo de una solicitud/orden")
async def order_timeline(
    order_id: str,
    scope: AdminScope = Depends(get_admin_scope),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    order = await _order_in_scope(db, scope, order_id)
    events = await OrderEventRepository(db).find_for_order(order_id)
    return {
        "order": OrderResponse.model_validate(order).model_dump(),
        "events": [OrderEventOut.model_validate(e).model_dump() for e in events],
    }
