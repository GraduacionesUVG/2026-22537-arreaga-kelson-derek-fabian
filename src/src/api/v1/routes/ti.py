"""Panel Dev/TI (solo superadmin): restaurantes, usuarios, estaciones y consola general."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import AdminScope, get_auth_service, require_superadmin
from src.core.database import get_db
from src.core.timeutils import local_today
from src.integrations.email_client import EmailSendError
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuRepository
from src.repositories.order_event_repo import OrderEventRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.admin import AdminUserCreateIn, AdminUserOut, EventsDayOut, KdsUserOut, RestaurantCreateIn
from src.services.auth_service import AuthService
from src.services.kds_admin_service import KdsAdminService
from src.services.ti_service import TiError, TiService

router = APIRouter()


def get_ti_service(
    db: AsyncIOMotorDatabase = Depends(get_db), auth_service: AuthService = Depends(get_auth_service)
) -> TiService:
    return TiService(db, auth_service)


@router.get("/restaurants", summary="Listar restaurantes")
async def list_restaurants(
    scope: AdminScope = Depends(require_superadmin), service: TiService = Depends(get_ti_service)
):
    return await service.list_restaurants()


@router.post("/restaurants", status_code=status.HTTP_201_CREATED, summary="Crear restaurante y su administrador")
async def create_restaurant(
    data: RestaurantCreateIn,
    scope: AdminScope = Depends(require_superadmin),
    service: TiService = Depends(get_ti_service),
):
    try:
        return await service.create_restaurant(data.model_dump())
    except TiError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/restaurant-users", response_model=list[AdminUserOut], summary="Usuarios administradores de restaurantes")
async def list_restaurant_users(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(require_superadmin),
    service: TiService = Depends(get_ti_service),
):
    return await service.list_admins(None if restaurant_id in (None, "", "all") else restaurant_id)


@router.post("/restaurant-users", status_code=status.HTTP_201_CREATED, summary="Crear admin para un restaurante existente")
async def create_restaurant_user(
    data: AdminUserCreateIn,
    scope: AdminScope = Depends(require_superadmin),
    service: TiService = Depends(get_ti_service),
):
    """Si no se envía `password`, se manda por correo el enlace para crear la contraseña.
    Con `password`, el usuario debe cambiarla en su primer login."""
    try:
        return await service.create_admin(data.model_dump())
    except TiError as exc:
        code = status.HTTP_404_NOT_FOUND if "no encontrado" in str(exc) else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(exc)) from exc


@router.post("/restaurant-users/{admin_id}/password-reset", summary="Enviar enlace de cambio de contraseña")
async def send_password_reset(
    admin_id: str,
    scope: AdminScope = Depends(require_superadmin),
    service: TiService = Depends(get_ti_service),
):
    try:
        return await service.send_password_reset(admin_id)
    except TiError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except EmailSendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No se pudo enviar el correo.") from exc


@router.get("/kds-users", response_model=list[KdsUserOut], summary="Estaciones KDS de todos los restaurantes")
async def list_all_kds(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(require_superadmin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    service = KdsAdminService(KdsUserRepository(db), RestaurantRepository(db), MenuRepository(db))
    return await service.list_stations(None if restaurant_id in (None, "", "all") else restaurant_id)


@router.get("/events", response_model=EventsDayOut, summary="Consola general de eventos por día")
async def list_all_events(
    on_date: date | None = Query(default=None, alias="date"),
    restaurant_id: str | None = Query(default="all"),
    kds_user_id: str | None = None,
    scope: AdminScope = Depends(require_superadmin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    day = on_date or local_today()
    rid = None if restaurant_id in (None, "", "all") else restaurant_id
    events = await OrderEventRepository(db).find_for_day(day.isoformat(), rid, kds_user_id)
    return {"date": day.isoformat(), "live": day == local_today(), "events": events}
