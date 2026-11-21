"""Panel de administración: plantillas, importación y exportación de menú y calendario (Excel)."""

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.api.v1.deps import AdminScope, get_admin_scope, get_menu_service
from src.core.database import get_db
from src.core.timeutils import local_today
from src.repositories.menu_repo import MenuRepository
from src.repositories.order_repo import OrderRepository
from src.schemas.admin import ImportResult, MenuItemOut
from src.services.menu_excel import (
    ImportValidationError,
    ImportWarnings,
    MenuImportService,
    build_calendar_workbook,
    build_menu_workbook,
)
from src.services.menu_service import MenuService

router = APIRouter()

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_FILE_BYTES = 5 * 1024 * 1024


def get_import_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> MenuImportService:
    return MenuImportService(MenuRepository(db), OrderRepository(db))


def _xlsx(content: bytes, filename: str) -> Response:
    return Response(content, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "El archivo supera 5 MB.")
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El archivo está vacío.")
    return content


def _validation_error(exc: ImportValidationError) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        {"message": "El archivo tiene errores. No se guardó nada.", "errors": exc.errors},
    )


# --- Menú general -------------------------------------------------------------------


@router.get("/menu/template", summary="Descargar plantilla del menú general")
async def menu_template(scope: AdminScope = Depends(get_admin_scope)):
    return _xlsx(build_menu_workbook(example=True), "plantilla_menu.xlsx")


@router.get("/menu/export", summary="Descargar menú general actual (sin calendario)")
async def menu_export(
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuImportService = Depends(get_import_service),
):
    content = await service.export_menu(scope.resolve_restaurant(restaurant_id))
    return _xlsx(content, f"menu_{local_today().isoformat()}.xlsx")


@router.post("/menu/import", response_model=ImportResult, summary="Importar menú general (crea o actualiza por código)")
async def menu_import(
    file: UploadFile = File(...),
    dry_run: bool = False,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuImportService = Depends(get_import_service),
):
    content = await _read_upload(file)
    try:
        return await service.import_menu(scope.resolve_restaurant(restaurant_id), content, dry_run)
    except ImportValidationError as exc:
        raise _validation_error(exc) from exc


# --- Calendario ----------------------------------------------------------------------


@router.get("/menu/calendar/template", summary="Descargar plantilla del calendario")
async def calendar_template(scope: AdminScope = Depends(get_admin_scope)):
    return _xlsx(build_calendar_workbook(example=True), "plantilla_calendario.xlsx")


@router.get("/menu/calendar/export", summary="Descargar calendario (default: desde hoy)")
async def calendar_export(
    restaurant_id: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuImportService = Depends(get_import_service),
):
    start = (from_date or local_today()).isoformat()
    content = await service.export_calendar(
        scope.resolve_restaurant(restaurant_id), start, to_date.isoformat() if to_date else None
    )
    return _xlsx(content, f"calendario_{start}.xlsx")


@router.get("/menu/calendar", response_model=list[MenuItemOut], summary="Listar menús de calendario")
async def calendar_list(
    restaurant_id: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    scope: AdminScope = Depends(get_admin_scope),
    menu_service: MenuService = Depends(get_menu_service),
):
    items = await menu_service.repo.find_calendar(
        scope.resolve_restaurant(restaurant_id),
        (from_date or local_today()).isoformat(),
        to_date.isoformat() if to_date else None,
    )
    return sorted(items, key=lambda i: (i.get("serve_date") or "", i.get("option_number") or 0))


@router.post(
    "/menu/calendar/import",
    response_model=ImportResult,
    summary="Actualizar calendario (agrega; avisa si una fecha ya tiene menú)",
    responses={409: {"description": "Hay fechas con menú existente. Repetir con accept_warnings=true para reemplazarlas."}},
)
async def calendar_import(
    file: UploadFile = File(...),
    dry_run: bool = False,
    accept_warnings: bool = False,
    restaurant_id: str | None = None,
    scope: AdminScope = Depends(get_admin_scope),
    service: MenuImportService = Depends(get_import_service),
):
    content = await _read_upload(file)
    try:
        return await service.import_calendar(scope.resolve_restaurant(restaurant_id), content, dry_run, accept_warnings)
    except ImportValidationError as exc:
        raise _validation_error(exc) from exc
    except ImportWarnings as exc:
        has_menu = any(w.get("type") == "date_has_menu" for w in exc.warnings)
        message = (
            "Hay fechas que ya tienen menú. Verifica el Excel para evitar sobrescrituras. "
            if has_menu
            else "El archivo incluye fechas que ya pasaron. "
        ) + "Para continuar, envía el mismo archivo con accept_warnings=true."
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": message, "warnings": exc.warnings}) from exc
