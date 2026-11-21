"""Panel de administración: estaciones KDS del restaurante."""

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import AdminScope, get_admin_scope
from src.core.database import get_db
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.admin import (
    KdsActiveIn,
    KdsPinIn,
    KdsPriorityIn,
    KdsStatusOut,
    KdsUserCreate,
    KdsUsernameIn,
    KdsUserOut,
    MenuFilterIn,
)
from src.services.kds_admin_service import KdsAdminError, KdsAdminService

router = APIRouter()


def get_kds_admin_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> KdsAdminService:
    return KdsAdminService(KdsUserRepository(db), RestaurantRepository(db), MenuRepository(db))


async def _station(service: KdsAdminService, scope: AdminScope, kds_user_id: str) -> dict:
    restaurant_id = None if scope.is_superadmin else scope.resolve_restaurant(None)
    station = await service.get(restaurant_id, kds_user_id)
    if station is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario KDS no encontrado.")
    return station


def _bad_request(exc: KdsAdminError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/kds-users", response_model=list[KdsUserOut], summary="Listar estaciones KDS")
async def list_kds_users(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    return await service.list_stations(scope.resolve_restaurant(restaurant_id, allow_all=True))


@router.get("/kds-users/status", response_model=list[KdsStatusOut], summary="Estado de conexión de las estaciones")
async def kds_status(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    return await service.statuses(scope.resolve_restaurant(restaurant_id, allow_all=True))


@router.post("/kds-users", response_model=KdsUserOut, status_code=status.HTTP_201_CREATED, summary="Crear estación KDS")
async def create_kds_user(
    data: KdsUserCreate,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    try:
        return await service.create(scope.resolve_restaurant(restaurant_id), data.model_dump())
    except KdsAdminError as exc:
        raise _bad_request(exc) from exc


@router.put("/kds-users/{kds_user_id}/username", response_model=KdsUserOut, summary="Cambiar usuario del KDS")
async def update_username(
    kds_user_id: str,
    data: KdsUsernameIn,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    station = await _station(service, scope, kds_user_id)
    try:
        return await service.update_username(station, data.username)
    except KdsAdminError as exc:
        raise _bad_request(exc) from exc


@router.put("/kds-users/{kds_user_id}/pin", response_model=KdsUserOut, summary="Cambiar PIN del KDS")
async def update_pin(
    kds_user_id: str,
    data: KdsPinIn,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    return await service.update_pin(await _station(service, scope, kds_user_id), data.pin)


@router.put("/kds-users/{kds_user_id}/priority", response_model=KdsUserOut, summary="Cambiar prioridad")
async def update_priority(
    kds_user_id: str,
    data: KdsPriorityIn,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    return await service.update_priority(await _station(service, scope, kds_user_id), data.priority)


@router.put("/kds-users/{kds_user_id}/menu-filter", response_model=KdsUserOut, summary="Productos que recibe la estación")
async def update_menu_filter(
    kds_user_id: str,
    data: MenuFilterIn,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    station = await _station(service, scope, kds_user_id)
    try:
        return await service.update_menu_filter(station, data.model_dump())
    except KdsAdminError as exc:
        raise _bad_request(exc) from exc


@router.put("/kds-users/{kds_user_id}/active", response_model=KdsUserOut, summary="Activar o pausar la estación")
async def update_active(
    kds_user_id: str,
    data: KdsActiveIn,
    scope: AdminScope = Depends(get_admin_scope),
    service: KdsAdminService = Depends(get_kds_admin_service),
):
    actor = {"type": "admin", "id": scope.admin_id, "username": scope.username or scope.email}
    return await service.update_active(await _station(service, scope, kds_user_id), data.active, actor)
