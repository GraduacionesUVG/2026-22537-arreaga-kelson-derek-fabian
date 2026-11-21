"""Panel de administración del restaurante: datos, horario, ubicación y menú (CRUD).

Todas las rutas usan el restaurante del token. Un superadmin indica `?restaurant_id=`.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import (
    AdminScope,
    get_admin_scope,
    get_menu_service,
    get_restaurant_service,
)
from src.core.database import get_db
from src.integrations.tilopay_client import TilopayAuthError, TilopayError
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.admin import (
    CategoryDetailOut,
    CategoryIconsOut,
    CategoryIn,
    CategoryOut,
    CategoryUpdate,
    LocationIn,
    MenuItemIn,
    MenuItemOut,
    MenuItemUpdate,
    OpeningHoursIn,
    PaymentGatewayIn,
    PaymentGatewayOut,
    RestaurantAdminOut,
    ServiceFeeIn,
)
from src.models.category_icons import CATEGORY_ICON_KEYS, DEFAULT_CATEGORY_ICON
from src.services.menu_service import (
    CategoryConflictError,
    CategoryInUseError,
    MenuService,
    MenuValidationError,
)
from src.services.payment_accounts import PaymentAccountError, PaymentAccountService, gateway_view
from src.services.restaurant_service import RestaurantService

router = APIRouter()


async def _restaurant_or_404(service: RestaurantService, restaurant_id: str) -> dict:
    restaurant = await service.get_restaurant(restaurant_id)
    if restaurant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Restaurante no encontrado.")
    return restaurant


# --- Restaurante -------------------------------------------------------------------


@router.get("/restaurant", response_model=RestaurantAdminOut, summary="Datos del restaurante")
async def get_restaurant(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    restaurant = await _restaurant_or_404(service, scope.resolve_restaurant(restaurant_id))
    return service.admin_view(restaurant)


@router.get("/restaurant/opening-hours", summary="Horario de atención (lunes a domingo)")
async def get_opening_hours(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    restaurant = await _restaurant_or_404(service, scope.resolve_restaurant(restaurant_id))
    return service.admin_view(restaurant)["opening_hours"]


@router.put("/restaurant/opening-hours", response_model=RestaurantAdminOut, summary="Editar horario")
async def update_opening_hours(
    data: OpeningHoursIn,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    rid = scope.resolve_restaurant(restaurant_id)
    await _restaurant_or_404(service, rid)
    updated = await service.update_opening_hours(rid, data.model_dump())
    return service.admin_view(updated)


@router.put("/restaurant/location", response_model=RestaurantAdminOut, summary="Editar ubicación (texto)")
async def update_location(
    data: LocationIn,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    rid = scope.resolve_restaurant(restaurant_id)
    await _restaurant_or_404(service, rid)
    updated = await service.update_location(rid, data.location)
    return service.admin_view(updated)


@router.get("/restaurant/service-fee", response_model=ServiceFeeIn, summary="Tarifa de servicio por pedido")
async def get_service_fee(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    restaurant = await _restaurant_or_404(service, scope.resolve_restaurant(restaurant_id))
    return {"service_fee": service.admin_view(restaurant)["service_fee"]}


@router.put("/restaurant/service-fee", response_model=RestaurantAdminOut, summary="Editar tarifa de servicio")
async def update_service_fee(
    data: ServiceFeeIn,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    """Monto fijo (Q, 2 decimales) que se le suma al cliente en cada pedido; 0 = sin tarifa.
    Solo aplica a pedidos creados después del cambio (los anteriores guardan la suya)."""
    rid = scope.resolve_restaurant(restaurant_id)
    await _restaurant_or_404(service, rid)
    updated = await service.update_service_fee(rid, data.service_fee)
    return service.admin_view(updated)


# --- Cuenta de pagos (Tilopay) --------------------------------------------------------


def get_payment_account_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> PaymentAccountService:
    return PaymentAccountService(RestaurantRepository(db))


@router.get(
    "/restaurant/payment-gateway", response_model=PaymentGatewayOut, summary="Estado de la cuenta de Tilopay"
)
async def get_payment_gateway(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
):
    """Si el restaurante ya tiene su cuenta de Tilopay. Nunca devuelve la contraseña ni la API key."""
    restaurant = await _restaurant_or_404(service, scope.resolve_restaurant(restaurant_id))
    return gateway_view(restaurant)


@router.put(
    "/restaurant/payment-gateway", response_model=PaymentGatewayOut, summary="Configurar la cuenta de Tilopay"
)
async def set_payment_gateway(
    data: PaymentGatewayIn,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
    accounts: PaymentAccountService = Depends(get_payment_account_service),
):
    """Guarda las credenciales de la cuenta de Tilopay del restaurante (cifradas).

    Antes de guardar hace login en Tilopay: 422 si Tilopay rechaza el usuario o la contraseña,
    502 si Tilopay no responde. La API key no se puede validar sin crear un link: se verifica
    con el primer pedido. Reemplaza la cuenta anterior; los links ya enviados con la cuenta
    anterior se deben revisar en esa cuenta.
    """
    rid = scope.resolve_restaurant(restaurant_id)
    await _restaurant_or_404(service, rid)
    try:
        updated = await accounts.save_tilopay(
            rid,
            api_user=data.api_user,
            api_password=data.api_password,
            api_key=data.api_key,
            currency=data.currency,
            actor={"type": "admin", "id": scope.admin_id, "username": scope.username},
        )
    except TilopayAuthError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Tilopay rechazó el usuario o la contraseña de la API.") from exc
    except TilopayError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No se pudo contactar a Tilopay. Intenta de nuevo.") from exc
    except PaymentAccountError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return gateway_view(updated)


@router.delete(
    "/restaurant/payment-gateway", status_code=status.HTTP_204_NO_CONTENT, summary="Quitar la cuenta de Tilopay"
)
async def delete_payment_gateway(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: RestaurantService = Depends(get_restaurant_service),
    accounts: PaymentAccountService = Depends(get_payment_account_service),
):
    """Borra las credenciales. En PRODUCTION el restaurante ya no podrá aceptar pedidos hasta configurar otra cuenta."""
    rid = scope.resolve_restaurant(restaurant_id)
    await _restaurant_or_404(service, rid)
    await accounts.clear(rid)


# --- Menú (CRUD) ----------------------------------------------------------------------


@router.get("/menu", response_model=list[MenuItemOut], summary="Listar items de menú")
async def list_menu(
    restaurant_id: str | None = None,
    category: str | None = None,
    schedule_type: str | None = Query(default=None, description="always | weekly | date"),
    on_date: date | None = Query(default=None, alias="date", description="Solo lo que se ofrece ese día"),
    include_unavailable: bool = True,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    return await service.admin_list(
        scope.resolve_restaurant(restaurant_id), category, schedule_type, on_date, include_unavailable
    )


@router.get("/menu/categories", response_model=list[str], summary="Categorías del restaurante")
async def list_categories(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    """Categorías creadas desde el panel más las que ya usa algún producto."""
    return await service.list_categories(scope.resolve_restaurant(restaurant_id))


@router.get(
    "/menu/categories/details",
    response_model=list[CategoryDetailOut],
    summary="Categorías con icono y cantidad de productos",
)
async def list_category_details(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    """Igual que `/menu/categories` pero con `icon` (el guardado o el sugerido por el nombre) y `products_count`."""
    return await service.list_category_details(scope.resolve_restaurant(restaurant_id))


@router.get("/menu/category-icons", response_model=CategoryIconsOut, summary="Claves de icono válidas")
async def list_category_icons(scope: AdminScope = Depends(get_admin_scope)):
    return {"icons": list(CATEGORY_ICON_KEYS), "default": DEFAULT_CATEGORY_ICON}


@router.post(
    "/menu/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED, summary="Crear categoría"
)
async def create_category(
    data: CategoryIn,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    """409 si ya existe (no distingue mayúsculas/minúsculas)."""
    try:
        return await service.create_category(scope.resolve_restaurant(restaurant_id), data.name, data.icon)
    except CategoryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MenuValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.patch("/menu/categories/{name}", response_model=CategoryOut, summary="Cambiar nombre o icono de una categoría")
async def update_category(
    name: str,
    data: CategoryUpdate,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    """Cambia el icono y/o renombra la categoría. Renombrar actualiza en cascada los productos y los
    filtros de menú de las estaciones KDS. 409 si el nombre nuevo ya existe (sin distinguir mayúsculas),
    422 si el icono no existe o no se envía nada, 404 si la categoría no existe."""
    if data.name is None and data.icon is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Indica el nombre nuevo o el icono.")
    try:
        updated = await service.update_category(scope.resolve_restaurant(restaurant_id), name, data.name, data.icon)
    except CategoryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except MenuValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Categoría no encontrada.")
    return updated


@router.delete("/menu/categories/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar categoría")
async def delete_category(
    name: str,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    """Solo si ningún producto la usa. Si aún tiene productos responde 409 con
    `detail = {message, category, products_count, products: [{id, code, name}]}`."""
    try:
        deleted = await service.delete_category(scope.resolve_restaurant(restaurant_id), name)
    except CategoryInUseError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": str(exc),
                "category": exc.category,
                "products_count": len(exc.items),
                "products": exc.items,
            },
        ) from exc
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Categoría no encontrada.")


@router.post("/menu", response_model=MenuItemOut, status_code=status.HTTP_201_CREATED, summary="Crear item")
async def create_menu_item(
    data: MenuItemIn,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    try:
        return await service.create_item(scope.resolve_restaurant(restaurant_id), data.model_dump())
    except MenuValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/menu/{item_id}", response_model=MenuItemOut, summary="Detalle de un item")
async def get_menu_item(
    item_id: str,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    item = await service.get_item(scope.resolve_restaurant(restaurant_id), item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item no encontrado.")
    return item


@router.put("/menu/{item_id}", response_model=MenuItemOut, summary="Editar item")
async def update_menu_item(
    item_id: str,
    data: MenuItemUpdate,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    try:
        item = await service.update_item(
            scope.resolve_restaurant(restaurant_id), item_id, data.model_dump(exclude_unset=True)
        )
    except MenuValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item no encontrado.")
    return item


@router.delete("/menu/{item_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar item")
async def delete_menu_item(
    item_id: str,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuService = Depends(get_menu_service),
):
    if not await service.delete_item(scope.resolve_restaurant(restaurant_id), item_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item no encontrado.")
