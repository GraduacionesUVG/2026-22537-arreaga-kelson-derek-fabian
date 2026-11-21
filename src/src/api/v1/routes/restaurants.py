"""
Rutas de administración de restaurantes (Módulo D).

GET/POST implementados reutilizando RestaurantService, mismo patrón que
src/api/v1/routes/menus.py. El id devuelto por GET /api/restaurants/ es el
que se usa como `restaurant_id` en GET /api/menus/?restaurant_id=<id>.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.v1.deps import AdminScope, get_restaurant_service, require_internal_api_key_strict, require_superadmin
from src.schemas.restaurant import RestaurantCreate, RestaurantResponse
from src.services.restaurant_service import RestaurantService

router = APIRouter()


@router.get("/", response_model=list[RestaurantResponse], dependencies=[Depends(require_internal_api_key_strict)])
async def list_restaurants(
    active_only: bool = False,
    service: RestaurantService = Depends(get_restaurant_service),
):
    return await service.list_public(active_only)


@router.get(
    "/{restaurant_id}", response_model=RestaurantResponse, dependencies=[Depends(require_internal_api_key_strict)]
)
async def get_restaurant(restaurant_id: str, service: RestaurantService = Depends(get_restaurant_service)):
    restaurant = await service.get_public(restaurant_id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Restaurante no encontrado.")
    return restaurant


@router.post("/", response_model=RestaurantResponse, status_code=status.HTTP_201_CREATED)
async def create_restaurant(
    data: RestaurantCreate,
    scope: AdminScope = Depends(require_superadmin),
    service: RestaurantService = Depends(get_restaurant_service),
):
    return await service.create_restaurant(data)
