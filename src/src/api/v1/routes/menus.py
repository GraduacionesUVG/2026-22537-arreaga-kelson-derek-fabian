"""
Rutas de catálogo de menús (Módulo D).

GET/POST están implementados de forma mínima reutilizando MenuService, para
mostrar el patrón aplicado a un segundo dominio; el resto (import masivo
CSV/Excel) queda como TODO.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.v1.deps import (
    AdminScope,
    get_admin_scope,
    get_menu_service,
    get_restaurant_service,
    require_internal_api_key_strict,
)
from src.schemas.menu import MenuItemCreate, MenuItemResponse
from src.services.menu_service import MenuService, MenuValidationError
from src.services.restaurant_service import RestaurantService

router = APIRouter()


@router.get("/", response_model=list[MenuItemResponse], dependencies=[Depends(require_internal_api_key_strict)])
async def list_menu_items(
    restaurant_id: str | None = None,
    service: MenuService = Depends(get_menu_service),
    restaurants: RestaurantService = Depends(get_restaurant_service),
):
    """Menú ofrecible hoy. Cada producto trae `restaurant_status` ("open"/"closed"):
    con "closed" el chatbot no debe ofrecer pedirlo (el endpoint de pedidos lo rechaza)."""
    items = await service.list_items(restaurant_id)
    availability = await restaurants.availability_by_id({i["restaurant_id"] for i in items if i.get("restaurant_id")})
    for item in items:
        status_ = availability.get(item.get("restaurant_id"))
        if status_:
            item["restaurant_status"] = status_["status"]
            item["restaurant_closed_reason"] = status_["closed_reason"]
            item["restaurant_status_message"] = status_["status_message"]
    return items


@router.post("/", response_model=MenuItemResponse)
async def create_menu_item(
    data: MenuItemCreate,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    """Requiere token de administración. El panel usa POST /api/admin/menu."""
    scope.resolve_restaurant(data.restaurant_id)
    try:
        return await service.create_item_from_schema(data)
    except MenuValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
