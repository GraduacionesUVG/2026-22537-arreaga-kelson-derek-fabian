"""Rutas propias de la pantalla KDS (token de KDS)."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import get_current_kds, get_menu_service
from src.core.database import get_db
from src.core.timeutils import local_today
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.admin import KdsActiveIn, MenuAvailabilityIn, MenuItemOut
from src.schemas.shift import CurrentShiftOut, ShiftKpisItemOut, ShiftOut, ShiftStartOut, ShiftSummaryOut
from src.services.kds_admin_service import KdsAdminService, kds_view
from src.services.menu_service import MenuService, MenuValidationError
from src.services.shift_service import ShiftError, ShiftService

router = APIRouter()


def _restaurant_id(kds_user: dict) -> str:
    restaurant_id = kds_user.get("restaurant_id")
    if not restaurant_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "La estación no tiene un restaurante asignado.")
    return restaurant_id


@router.get("/me", summary="Datos de la estación autenticada")
async def kds_me(kds_user: dict = Depends(get_current_kds)):
    return kds_view(kds_user)


@router.put("/me/active", summary="Pausar o reanudar la recepción de pedidos de la estación")
async def kds_set_active(
    data: KdsActiveIn,
    kds_user: dict = Depends(get_current_kds),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """`{"active": false}` pausa la estación (deja de recibir solicitudes nuevas; lo que ya
    tiene lo sigue atendiendo), `{"active": true}` la reanuda. Pausar y reanudar requieren
    un turno abierto hoy (409 `no_open_shift`); cada pausa queda registrada en el turno.
    Todas las pantallas con esta estación abierta reciben por WebSocket
    `{"type": "station_status", "active": ...}`."""
    if not await ShiftService(db).has_open_shift_today(kds_user["id"]):
        message = "Inicia el turno para recibir pedidos." if data.active else "No hay un turno en curso para pausar."
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": message, "code": "no_open_shift"})
    service = KdsAdminService(KdsUserRepository(db), RestaurantRepository(db), MenuRepository(db))
    return await service.update_active(kds_user, data.active, _kds_actor(kds_user))


@router.get("/menu", response_model=list[MenuItemOut], summary="Menú del restaurante de la estación")
async def kds_menu(
    category: str | None = None,
    kds_user: dict = Depends(get_current_kds),
    service: MenuService = Depends(get_menu_service),
):
    """Todos los items del restaurante (activos e inactivos). `active` es la
    disponibilidad efectiva e `inactive_mode` indica si la desactivación es
    `permanent` o `daily`."""
    return await service.admin_list(_restaurant_id(kds_user), category)


@router.patch(
    "/menu/{item_id}/availability", response_model=MenuItemOut, summary="Activar o desactivar un item del menú"
)
async def kds_set_availability(
    item_id: str,
    data: MenuAvailabilityIn,
    kds_user: dict = Depends(get_current_kds),
    service: MenuService = Depends(get_menu_service),
):
    """`{"active": true}` lo activa. `{"active": false, "mode": "permanent"}` lo deja
    inactivo hasta que alguien lo active; `"mode": "daily"` lo desactiva solo por
    hoy (hora de Guatemala) y vuelve a estar activo al día siguiente."""
    try:
        item = await service.set_availability(_restaurant_id(kds_user), item_id, data.active, data.mode)
    except MenuValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ValueError:
        item = None  # item_id no es un ObjectId válido
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item no encontrado.")
    return item


# --- Turnos -------------------------------------------------------------------------------------


def _kds_actor(kds_user: dict) -> dict:
    return {"type": "kds", "id": kds_user["id"], "username": kds_user.get("username")}


def _conflict(exc: ShiftError) -> HTTPException:
    detail = exc.detail()
    if detail.get("shift"):
        detail["shift"] = ShiftOut.model_validate(detail["shift"]).model_dump(mode="json")
    return HTTPException(status.HTTP_409_CONFLICT, detail)


@router.get("/shift/current", response_model=CurrentShiftOut, summary="Turno abierto de la estación")
async def kds_current_shift(kds_user: dict = Depends(get_current_kds), db: AsyncIOMotorDatabase = Depends(get_db)):
    """`shift = null`: no hay turno abierto (mostrar "Iniciar turno"). Con turno abierto trae los
    KPIs parciales. `pendientes` sirve para el mensaje de confirmación de "Finalizar turno"."""
    return await ShiftService(db).current(kds_user)


@router.post(
    "/shift/start",
    response_model=ShiftStartOut,
    status_code=status.HTTP_201_CREATED,
    summary="Iniciar turno: activa la estación",
)
async def kds_start_shift(kds_user: dict = Depends(get_current_kds), db: AsyncIOMotorDatabase = Depends(get_db)):
    """Abre el turno y activa la estación. Si había un turno abierto de un día anterior, lo cierra
    (`previous_shift_closed`). 409 `shift_already_open` si ya hay un turno abierto hoy."""
    _restaurant_id(kds_user)
    try:
        return await ShiftService(db).start(kds_user, _kds_actor(kds_user))
    except ShiftError as exc:
        raise _conflict(exc) from exc


@router.post("/shift/end", response_model=ShiftSummaryOut, summary="Finalizar turno: pausa la estación y devuelve el resumen")
async def kds_end_shift(kds_user: dict = Depends(get_current_kds), db: AsyncIOMotorDatabase = Depends(get_db)):
    """Cierra el turno, pausa la estación y responde el resumen (tickets + KPIs).
    409 `no_open_shift` o `pending_requests` (con `por_aceptar`)."""
    try:
        return await ShiftService(db).end(kds_user, _kds_actor(kds_user))
    except ShiftError as exc:
        raise _conflict(exc) from exc


@router.get("/shifts", response_model=list[ShiftKpisItemOut], summary="Turnos de la estación en un día")
async def kds_shifts(
    on_date: date | None = Query(default=None, alias="date", description="YYYY-MM-DD, default hoy"),
    kds_user: dict = Depends(get_current_kds),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    day = on_date or local_today()
    return await ShiftService(db).station_shifts_for_day(kds_user["id"], day.isoformat())


@router.get("/shifts/{shift_id}", response_model=ShiftSummaryOut, summary="Resumen de un turno de la estación")
async def kds_shift_summary(
    shift_id: str, kds_user: dict = Depends(get_current_kds), db: AsyncIOMotorDatabase = Depends(get_db)
):
    service = ShiftService(db)
    shift = await service.get_shift(shift_id, kds_user_id=kds_user["id"])
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Turno no encontrado.")
    return await service.summary(shift)
