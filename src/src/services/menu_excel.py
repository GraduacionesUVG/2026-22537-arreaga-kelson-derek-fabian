"""
Importación y exportación de menús en Excel.

Dos plantillas independientes:

- **Menú general** (hojas `menu`, `modificadores`, `instrucciones`): productos
  fijos (`tipo = fijo`) y semanales (`tipo = semanal` + `dia_semana`).
  Se importa por `codigo`: crea o actualiza.
- **Calendario** (hojas `calendario`, `modificadores`, `instrucciones`): menús
  por fecha. Se agregan; si una fecha ya tiene menú se devuelve un aviso y,
  al confirmarlo (`accept_warnings`), se reemplaza el menú de esa fecha.

La importación es todo o nada. La exportación usa el mismo formato para
poder editar el archivo y volver a subirlo.
"""

import io
import re
from datetime import date, datetime, time

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.core.timeutils import local_today, utc_now
from src.models.menu import (
    DEFAULT_CALENDAR_CATEGORY,
    DEFAULT_CALENDAR_NAME,
    ScheduleType,
    resolve_schedule_type,
)
from src.models.order import OrderPipelineStatus
from src.models.restaurant import WEEKDAYS
from src.repositories.menu_repo import MenuRepository
from src.repositories.order_repo import OrderRepository
from src.services.menu_service import (
    MenuService,
    MenuValidationError,
    find_name_conflict,
    normalize_item,
)

DAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_DAY_ALIASES = {
    **{es: en for es, en in zip(DAYS_ES, WEEKDAYS)},
    "miercoles": "wednesday",
    "sabado": "saturday",
    **{en: en for en in WEEKDAYS},
}

MENU_COLUMNS = [
    "codigo", "nombre", "categoria", "descripcion", "precio", "tipo", "dia_semana", "periodo",
    "tiempo_prep_min", "listo_desde", "hora_limite_pedido", "porciones_dia", "disponible", "solo_en_local",
]
CALENDAR_COLUMNS = [
    "fecha", "dia_semana", "opcion", "nombre", "categoria", "descripcion", "precio", "tiempo_prep_min",
    "periodo", "listo_desde", "hora_limite_pedido", "porciones_dia", "disponible", "codigo",
]
MODIFIER_COLUMNS = ["codigo_item", "grupo", "nombre", "precio_extra"]

MENU_REQUIRED = {"nombre", "categoria", "precio", "tipo"}
CALENDAR_REQUIRED = {"fecha", "descripcion", "precio"}

MENU_INSTRUCTIONS = [
    ("Hoja 'menu'", "Una fila por producto. Si el código ya existe, la fila actualiza ese producto."),
    ("codigo", "Identificador único del producto en el restaurante (ej. HAMB-01). Si se deja vacío se genera."),
    ("nombre / categoria", "Obligatorios. El nombre no se puede repetir entre productos que se ofrecen el mismo día."),
    ("descripcion", "Opcional."),
    ("precio", "Obligatorio, en quetzales sin símbolo (ej. 25.00)."),
    ("tipo", "fijo = todos los días; semanal = solo el dia_semana indicado."),
    ("dia_semana", "Solo para tipo semanal: lunes, martes, miércoles, jueves, viernes, sábado o domingo."),
    ("periodo", "Opcional: desayuno, almuerzo o cena."),
    ("tiempo_prep_min", "Minutos para preparar una unidad (default 5)."),
    ("listo_desde", "Opcional, HH:MM. Se avisa al estudiante que está listo aprox. desde esa hora."),
    ("hora_limite_pedido", "Opcional, HH:MM. Después de esa hora ya no se ofrece ese día."),
    ("porciones_dia", "Opcional. Porciones disponibles por día; vacío = sin límite."),
    ("disponible / solo_en_local", "SI o NO (default SI / NO)."),
    ("Hoja 'modificadores'", "Una fila por modificador: codigo_item, grupo (opcional), nombre, precio_extra (0.00 si no tiene costo)."),
    ("", "Los modificadores de un producto que aparece en esta hoja reemplazan los que tenía."),
    ("Menús por fecha", "Se cargan con la plantilla de calendario, no en esta hoja."),
]
CALENDAR_INSTRUCTIONS = [
    ("Hoja 'calendario'", "Una fila por opción de menú y fecha. Se agrega al menú existente."),
    ("fecha", "Obligatoria, formato dd/mm/aaaa. El producto solo se ofrece ese día."),
    ("dia_semana", "Opcional; si se llena debe coincidir con la fecha."),
    ("opcion", "Número de opción (1, 2, 3...). El nombre final queda como 'nombre - Opción N'."),
    ("nombre", f"Opcional, default '{DEFAULT_CALENDAR_NAME}'."),
    ("categoria", f"Opcional, default '{DEFAULT_CALENDAR_CATEGORY}'. Permite asignar estos menús a una estación KDS."),
    ("descripcion", "Obligatoria. Ej. 'Cerdo al curry + Arroz blanco + Ensalada verde'."),
    ("precio", "Obligatorio, en quetzales sin símbolo."),
    ("tiempo_prep_min", "Minutos de preparación (default 10)."),
    ("periodo", "Opcional, default almuerzo."),
    ("listo_desde / hora_limite_pedido", "Opcionales, HH:MM."),
    ("porciones_dia", "Opcional; vacío = sin límite."),
    ("disponible", "SI o NO (default SI)."),
    ("codigo", "Opcional; se genera como CAL-AAAAMMDD-N."),
    ("Fechas repetidas", "Si una fecha ya tiene menú, el sistema avisa antes de reemplazarlo."),
    ("Hoja 'modificadores'", "Opcional. codigo_item debe coincidir con la columna codigo."),
]


class ImportValidationError(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("El archivo tiene errores.")


class ImportWarnings(Exception):
    def __init__(self, warnings: list[dict]):
        self.warnings = warnings
        super().__init__("Hay fechas que ya tienen menú.")


# --- Lectura de celdas -------------------------------------------------------------


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _number(value, field: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("Q", "").replace(",", "").strip())
    except ValueError:
        raise MenuValidationError(f"{field} debe ser numérico.") from None


def _integer(value, field: str) -> int | None:
    number = _number(value, field)
    if number is None:
        return None
    if not float(number).is_integer():
        raise MenuValidationError(f"{field} debe ser un número entero.")
    return int(number)


def _yes_no(value, default: bool, field: str) -> bool:
    text = (_text(value) or "").lower()
    if not text:
        return default
    if text in ("si", "sí", "s", "yes", "true", "1", "x"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    raise MenuValidationError(f"{field} debe ser SI o NO.")


def _hhmm(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    text = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if not match:
        raise MenuValidationError(f"{field} debe tener formato HH:MM.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _date(value, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        raise MenuValidationError(f"{field} es obligatoria.")
    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise MenuValidationError(f"{field} debe tener formato dd/mm/aaaa.")


def _weekday(value, field: str) -> str | None:
    text = (_text(value) or "").lower()
    if not text:
        return None
    if text not in _DAY_ALIASES:
        raise MenuValidationError(f"{field} no es un día válido.")
    return _DAY_ALIASES[text]


def _read_sheet(workbook, name: str, columns: list[str], required: bool = True) -> tuple[list[tuple[int, dict]], list[dict]]:
    errors: list[dict] = []
    if name not in workbook.sheetnames:
        if required:
            errors.append({"sheet": name, "row": None, "column": None, "error": f"Falta la hoja '{name}'."})
        return [], errors
    sheet = workbook[name]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return [], errors
    header = [(_text(h) or "").lower() for h in rows[0]]
    missing = [c for c in columns if c not in header and c not in ("dia_semana", "codigo", "opcion", "grupo")]
    for column in missing:
        errors.append({"sheet": name, "row": 1, "column": column, "error": f"Falta la columna '{column}'."})
    data = []
    for index, row in enumerate(rows[1:], start=2):
        if all(v in (None, "") for v in row):
            continue
        data.append((index, {header[i]: row[i] for i in range(min(len(header), len(row))) if header[i]}))
    return data, errors


def _parse_modifiers(workbook, errors: list[dict]) -> dict[str, list[dict]]:
    rows, sheet_errors = _read_sheet(workbook, "modificadores", MODIFIER_COLUMNS, required=False)
    errors.extend(sheet_errors)
    result: dict[str, list[dict]] = {}
    for row_number, row in rows:
        code = _text(row.get("codigo_item"))
        name = _text(row.get("nombre"))
        try:
            if not code:
                raise MenuValidationError("codigo_item es obligatorio.")
            if not name:
                raise MenuValidationError("nombre es obligatorio.")
            price = _number(row.get("precio_extra"), "precio_extra") or 0.0
            if price < 0:
                raise MenuValidationError("precio_extra no puede ser negativo.")
        except MenuValidationError as exc:
            errors.append({"sheet": "modificadores", "row": row_number, "column": None, "error": str(exc)})
            continue
        result.setdefault(code, []).append({"group": _text(row.get("grupo")), "name": name, "price": price})
    return result


# --- Escritura de plantillas ------------------------------------------------------------


def _write_sheet(workbook: Workbook, title: str, columns: list[str], rows: list[list], first: bool = False):
    sheet = workbook.active if first else workbook.create_sheet(title)
    sheet.title = title
    sheet.append(columns)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    for row in rows:
        sheet.append(row)
    for index, column in enumerate(columns, start=1):
        width = max([len(column)] + [len(str(r[index - 1])) for r in rows if r[index - 1] is not None] + [8])
        sheet.column_dimensions[get_column_letter(index)].width = min(60, width + 2)
    sheet.freeze_panes = "A2"
    return sheet


def _write_instructions(workbook: Workbook, lines: list[tuple[str, str]]) -> None:
    sheet = workbook.create_sheet("instrucciones")
    sheet.append(["Campo", "Descripción"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for line in lines:
        sheet.append(list(line))
    sheet.column_dimensions["A"].width = 34
    sheet.column_dimensions["B"].width = 110


def _to_bytes(workbook: Workbook) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _yes(value: bool) -> str:
    return "SI" if value else "NO"


def _base_calendar_name(item: dict) -> str:
    name = item.get("name") or DEFAULT_CALENDAR_NAME
    return re.sub(r"\s*-\s*Opci[oó]n\s+\d+\s*$", "", name)


def _modifier_rows(items: list[dict]) -> list[list]:
    rows = []
    for item in items:
        for modifier in item.get("modifiers") or []:
            rows.append([item.get("code"), modifier.get("group"), modifier.get("name"), modifier.get("price", 0)])
    return rows


def build_menu_workbook(items: list[dict] | None = None, example: bool = False) -> bytes:
    items = items or []
    rows = []
    for item in items:
        schedule = resolve_schedule_type(item)
        day = item.get("day_of_week")
        rows.append(
            [
                item.get("code"), item.get("name"), item.get("category"), item.get("description"), item.get("price"),
                "semanal" if schedule == ScheduleType.WEEKLY.value else "fijo",
                DAYS_ES[WEEKDAYS.index(day)] if schedule == ScheduleType.WEEKLY.value and day in WEEKDAYS else None,
                item.get("meal_period"), item.get("prep_time_minutes"), item.get("ready_from"),
                item.get("order_cutoff"), item.get("daily_stock"), _yes(item.get("available", True)),
                _yes(item.get("dine_in_only", False)),
            ]
        )
    if example:
        rows = [["HAMB-01", "Hamburguesa", "comida", "Carne, queso y vegetales", 25.0, "fijo", None, None, 8, None, None, None, "SI", "NO"]]
    workbook = Workbook()
    _write_sheet(workbook, "menu", MENU_COLUMNS, rows, first=True)
    modifiers = [["HAMB-01", "Quitar", "Sin tomate", 0.0], ["HAMB-01", "Extras", "Doble queso", 5.0]] if example else _modifier_rows(items)
    _write_sheet(workbook, "modificadores", MODIFIER_COLUMNS, modifiers)
    _write_instructions(workbook, MENU_INSTRUCTIONS)
    return _to_bytes(workbook)


def build_calendar_workbook(items: list[dict] | None = None, example: bool = False) -> bytes:
    items = items or []
    rows = []
    for item in items:
        serve = date.fromisoformat(item["serve_date"])
        rows.append(
            [
                serve.strftime("%d/%m/%Y"), DAYS_ES[serve.weekday()], item.get("option_number"),
                _base_calendar_name(item), item.get("category"), item.get("description"), item.get("price"),
                item.get("prep_time_minutes"), item.get("meal_period"), item.get("ready_from"),
                item.get("order_cutoff"), item.get("daily_stock"), _yes(item.get("available", True)), item.get("code"),
            ]
        )
    if example:
        rows = [
            ["25/09/2026", "viernes", 1, DEFAULT_CALENDAR_NAME, DEFAULT_CALENDAR_CATEGORY,
             "Churrasquito a la plancha + Frijol volteado + Arroz blanco", 40.0, 10, "almuerzo", "11:30", "10:00", None, "SI", None],
            ["25/09/2026", "viernes", 2, DEFAULT_CALENDAR_NAME, DEFAULT_CALENDAR_CATEGORY,
             "Hilachas de res + Arroz blanco + Ensalada verde", 40.0, 10, "almuerzo", "11:30", "10:00", None, "SI", None],
        ]
    workbook = Workbook()
    _write_sheet(workbook, "calendario", CALENDAR_COLUMNS, rows, first=True)
    _write_sheet(workbook, "modificadores", MODIFIER_COLUMNS, _modifier_rows(items))
    _write_instructions(workbook, CALENDAR_INSTRUCTIONS)
    return _to_bytes(workbook)


# --- Importación ------------------------------------------------------------------------


def _open(content: bytes):
    try:
        return load_workbook(io.BytesIO(content), data_only=True)
    except Exception as exc:
        raise ImportValidationError(
            [{"sheet": "-", "row": None, "column": None, "error": "No se pudo leer el archivo. Debe ser .xlsx."}]
        ) from exc


def _attach_modifiers(parsed: list[tuple[int, dict]], modifiers: dict[str, list[dict]], errors: list[dict], known_codes: set[str]):
    codes = {item["code"] for _, item in parsed if item.get("code")}
    for code in modifiers:
        if code not in codes and code not in known_codes:
            errors.append({"sheet": "modificadores", "row": None, "column": "codigo_item", "error": f"El código '{code}' no existe."})
    for _, item in parsed:
        if item.get("code") in modifiers:
            item["modifiers"] = modifiers[item["code"]]
            item["_modifiers_in_file"] = True


def _preview(restaurant_id: str, normalized) -> list[dict]:
    items = []
    for _, item in normalized:
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        clean["restaurant_id"] = restaurant_id
        clean["code"] = clean.get("code") or "(se genera)"
        for modifier in clean.get("modifiers") or []:
            modifier.setdefault("id", "")
        items.append(clean)
    return items


class MenuImportService:
    def __init__(self, menu_repo: MenuRepository, order_repo: OrderRepository):
        self.menu_repo = menu_repo
        self.order_repo = order_repo
        self.menu_service = MenuService(menu_repo, order_repo)

    # --- Menú general ---------------------------------------------------------------

    async def import_menu(self, restaurant_id: str, content: bytes, dry_run: bool = False) -> dict:
        workbook = _open(content)
        rows, errors = _read_sheet(workbook, "menu", MENU_COLUMNS)
        modifiers = _parse_modifiers(workbook, errors)
        existing = await self.menu_repo.find_for_restaurant(restaurant_id)
        existing_by_code = {i.get("code"): i for i in existing if i.get("code")}

        parsed: list[tuple[int, dict]] = []
        seen_codes: set[str] = set()
        for row_number, row in rows:
            try:
                for column in MENU_REQUIRED:
                    if _text(row.get(column)) is None:
                        raise MenuValidationError(f"{column} es obligatorio.")
                tipo = (_text(row.get("tipo")) or "").lower()
                if tipo == "fecha":
                    raise MenuValidationError("Los menús por fecha se cargan con la plantilla de calendario.")
                if tipo not in ("fijo", "semanal"):
                    raise MenuValidationError("tipo debe ser fijo o semanal.")
                item = {
                    "code": _text(row.get("codigo")),
                    "name": _text(row.get("nombre")),
                    "category": _text(row.get("categoria")),
                    "description": _text(row.get("descripcion")),
                    "price": _number(row.get("precio"), "precio"),
                    "schedule_type": ScheduleType.WEEKLY.value if tipo == "semanal" else ScheduleType.ALWAYS.value,
                    "day_of_week": _weekday(row.get("dia_semana"), "dia_semana"),
                    "meal_period": (_text(row.get("periodo")) or "").lower() or None,
                    "prep_time_minutes": _number(row.get("tiempo_prep_min"), "tiempo_prep_min"),
                    "ready_from": _hhmm(row.get("listo_desde"), "listo_desde"),
                    "order_cutoff": _hhmm(row.get("hora_limite_pedido"), "hora_limite_pedido"),
                    "daily_stock": _integer(row.get("porciones_dia"), "porciones_dia"),
                    "available": _yes_no(row.get("disponible"), True, "disponible"),
                    "dine_in_only": _yes_no(row.get("solo_en_local"), False, "solo_en_local"),
                }
                if tipo == "semanal" and not item["day_of_week"]:
                    raise MenuValidationError("dia_semana es obligatorio para tipo semanal.")
                if not item["code"]:
                    same_name = [
                        e for e in existing
                        if (e.get("name") or "").strip().lower() == item["name"].strip().lower()
                        and resolve_schedule_type(e) != ScheduleType.DATE.value
                        and e.get("code")
                    ]
                    if len(same_name) == 1:
                        item["code"] = same_name[0]["code"]
                if item["code"]:
                    if item["code"] in seen_codes:
                        raise MenuValidationError(f"El código '{item['code']}' está repetido en el archivo.")
                    seen_codes.add(item["code"])
                    current = existing_by_code.get(item["code"])
                    if current and resolve_schedule_type(current) == ScheduleType.DATE.value:
                        raise MenuValidationError(f"El código '{item['code']}' pertenece a un menú de calendario.")
                parsed.append((row_number, item))
            except MenuValidationError as exc:
                errors.append({"sheet": "menu", "row": row_number, "column": None, "error": str(exc)})

        _attach_modifiers(parsed, modifiers, errors, set(existing_by_code))
        normalized = self._normalize_all(parsed, existing, existing_by_code, errors, sheet="menu")
        if errors:
            raise ImportValidationError(errors)

        created = sum(1 for _, item in normalized if item.get("code") not in existing_by_code)
        result = {"dry_run": dry_run, "created": created, "updated": len(normalized) - created, "replaced_dates": [], "items": []}
        if dry_run:
            result["items"] = _preview(restaurant_id, normalized)
            return result

        # Modificadores en archivo para códigos que no vienen en la hoja menu
        extra_modifier_codes = set(modifiers) - {item.get("code") for _, item in normalized}
        saved = await self._save(restaurant_id, normalized, existing_by_code)
        now = utc_now()
        for code in extra_modifier_codes:
            current = existing_by_code[code]
            updated = await self.menu_repo.update_one(
                current["id"], {"modifiers": normalize_item({**current, "modifiers": modifiers[code]})["modifiers"], "updated_at": now}
            )
            saved.append(updated)
        result["items"] = saved
        return result

    def _normalize_all(self, parsed, existing, existing_by_code, errors, sheet):
        normalized: list[tuple[int, dict]] = []
        batch: list[dict] = []
        for row_number, item in parsed:
            try:
                current = existing_by_code.get(item.get("code")) if item.get("code") else None
                if current and not item.pop("_modifiers_in_file", False):
                    item["modifiers"] = current.get("modifiers") or []
                item.pop("_modifiers_in_file", None)
                clean = normalize_item(item)
                clean["code"] = item.get("code")
                clean["id"] = current["id"] if current else f"new-{row_number}"
                others = [e for e in existing if e["id"] not in {b["id"] for b in batch}] + batch
                conflict = find_name_conflict(clean, [o for o in others if not o.get("_replaced")])
                if conflict:
                    where = f"fila {conflict['_row']}" if conflict.get("_row") else f"código {conflict.get('code')}"
                    raise MenuValidationError(f"El nombre '{clean['name']}' ya se ofrece el mismo día ({where}).")
                clean["_row"] = row_number
                batch.append(clean)
                normalized.append((row_number, clean))
            except MenuValidationError as exc:
                errors.append({"sheet": sheet, "row": row_number, "column": None, "error": str(exc)})
        return normalized

    async def _save(self, restaurant_id: str, normalized, existing_by_code) -> list[dict]:
        now = utc_now()
        taken = set(existing_by_code)
        saved = []
        for _, item in normalized:
            item = {k: v for k, v in item.items() if not k.startswith("_")}
            item.pop("id")
            current = existing_by_code.get(item.get("code")) if item.get("code") else None
            if current:
                saved.append(await self.menu_repo.update_one(current["id"], item | {"updated_at": now}))
                continue
            if not item.get("code"):
                item["code"] = await self.menu_service.generate_code(restaurant_id, item["name"], taken)
            taken.add(item["code"])
            saved.append(
                await self.menu_repo.insert_one(
                    item | {"restaurant_id": restaurant_id, "deleted": False, "deleted_at": None, "created_at": now, "updated_at": now}
                )
            )
        return saved

    # --- Calendario --------------------------------------------------------------------

    async def import_calendar(
        self, restaurant_id: str, content: bytes, dry_run: bool = False, accept_warnings: bool = False
    ) -> dict:
        workbook = _open(content)
        rows, errors = _read_sheet(workbook, "calendario", CALENDAR_COLUMNS)
        modifiers = _parse_modifiers(workbook, errors)

        parsed: list[tuple[int, dict]] = []
        per_date_index: dict[str, int] = {}
        seen_codes: set[str] = set()
        for row_number, row in rows:
            try:
                for column in CALENDAR_REQUIRED:
                    if row.get(column) in (None, ""):
                        raise MenuValidationError(f"{column} es obligatorio.")
                serve = _date(row.get("fecha"), "fecha")
                day = _weekday(row.get("dia_semana"), "dia_semana")
                if day and day != WEEKDAYS[serve.weekday()]:
                    raise MenuValidationError(
                        f"dia_semana no coincide con la fecha ({serve.strftime('%d/%m/%Y')} es {DAYS_ES[serve.weekday()]})."
                    )
                option = _integer(row.get("opcion"), "opcion")
                base_name = _text(row.get("nombre")) or DEFAULT_CALENDAR_NAME
                name = base_name
                if option is not None and not re.search(r"opci[oó]n\s+\d+", base_name, re.IGNORECASE):
                    name = f"{base_name} - Opción {option}"
                iso = serve.isoformat()
                per_date_index[iso] = per_date_index.get(iso, 0) + 1
                code = _text(row.get("codigo")) or f"CAL-{serve.strftime('%Y%m%d')}-{option or per_date_index[iso]}"
                if code in seen_codes:
                    raise MenuValidationError(f"El código '{code}' está repetido en el archivo.")
                seen_codes.add(code)
                prep = _number(row.get("tiempo_prep_min"), "tiempo_prep_min")
                parsed.append(
                    (
                        row_number,
                        {
                            "code": code,
                            "name": name,
                            "category": _text(row.get("categoria")) or DEFAULT_CALENDAR_CATEGORY,
                            "description": _text(row.get("descripcion")),
                            "price": _number(row.get("precio"), "precio"),
                            "schedule_type": ScheduleType.DATE.value,
                            "serve_date": iso,
                            "option_number": option,
                            "meal_period": (_text(row.get("periodo")) or "almuerzo").lower(),
                            "prep_time_minutes": prep if prep is not None else 10.0,
                            "ready_from": _hhmm(row.get("listo_desde"), "listo_desde"),
                            "order_cutoff": _hhmm(row.get("hora_limite_pedido"), "hora_limite_pedido"),
                            "daily_stock": _integer(row.get("porciones_dia"), "porciones_dia"),
                            "available": _yes_no(row.get("disponible"), True, "disponible"),
                            "dine_in_only": False,
                        },
                    )
                )
            except MenuValidationError as exc:
                errors.append({"sheet": "calendario", "row": row_number, "column": None, "error": str(exc)})

        existing = await self.menu_repo.find_for_restaurant(restaurant_id)
        file_dates = sorted({item["serve_date"] for _, item in parsed})
        conflict_items = [
            i for i in existing if resolve_schedule_type(i) == ScheduleType.DATE.value and i.get("serve_date") in file_dates
        ]
        replaced_ids = {i["id"] for i in conflict_items}
        for item in existing:
            if item["id"] in replaced_ids:
                item["_replaced"] = True
        existing_by_code = {i.get("code"): i for i in existing if i.get("code")}
        for row_number, item in parsed:
            other = existing_by_code.get(item["code"])
            if other and other["id"] not in replaced_ids:
                errors.append({"sheet": "calendario", "row": row_number, "column": "codigo", "error": f"El código '{item['code']}' ya existe."})

        _attach_modifiers(parsed, modifiers, errors, set())
        active_existing = [i for i in existing if i["id"] not in replaced_ids]
        normalized = self._normalize_all(parsed, active_existing, {}, errors, sheet="calendario")
        if errors:
            raise ImportValidationError(errors)

        warnings = await self._calendar_warnings(restaurant_id, conflict_items, file_dates)
        if warnings and not accept_warnings and not dry_run:
            raise ImportWarnings(warnings)

        result = {
            "dry_run": dry_run,
            "created": len(normalized),
            "updated": 0,
            "replaced_dates": sorted({i["serve_date"] for i in conflict_items}),
            "warnings": warnings,
            "items": [],
        }
        if dry_run:
            result["items"] = _preview(restaurant_id, normalized)
            return result

        await self.menu_repo.soft_delete_many(list(replaced_ids), utc_now())
        result["items"] = await self._save(restaurant_id, normalized, {})
        return result

    async def _calendar_warnings(self, restaurant_id: str, conflict_items: list[dict], file_dates: list[str]) -> list[dict]:
        warnings = []
        by_date: dict[str, list[dict]] = {}
        for item in conflict_items:
            by_date.setdefault(item["serve_date"], []).append(item)
        for serve_date, items in sorted(by_date.items()):
            ids = {i["id"] for i in items}
            orders = await self.order_repo.find_all(
                {
                    "restaurant_id": restaurant_id,
                    "items.menu_item_id": {"$in": list(ids)},
                    "pipeline_status": {"$nin": [OrderPipelineStatus.REJECTED.value, OrderPipelineStatus.CANCELLED.value]},
                }
            )
            warnings.append(
                {
                    "type": "date_has_menu",
                    "date": serve_date,
                    "existing_items": [i["name"] for i in items],
                    "orders_count": len(orders),
                    "message": (
                        f"El {date.fromisoformat(serve_date).strftime('%d/%m/%Y')} ya tiene {len(items)} menú(s)"
                        + (f" y {len(orders)} pedido(s)" if orders else "")
                        + ". Verifica el Excel: si continúas, se reemplazará el menú de esa fecha."
                    ),
                }
            )
        today = local_today()
        for serve_date in file_dates:
            if date.fromisoformat(serve_date) < today:
                warnings.append(
                    {
                        "type": "past_date",
                        "date": serve_date,
                        "existing_items": [],
                        "orders_count": 0,
                        "message": f"La fecha {date.fromisoformat(serve_date).strftime('%d/%m/%Y')} ya pasó.",
                    }
                )
        return warnings

    # --- Exportación ---------------------------------------------------------------------

    async def _ensure_codes(self, restaurant_id: str) -> None:
        """Asigna código a los productos que no lo tienen (datos anteriores a la plantilla)."""
        items = await self.menu_repo.find_for_restaurant(restaurant_id)
        taken = {i["code"] for i in items if i.get("code")}
        for item in items:
            if not item.get("code"):
                code = await self.menu_service.generate_code(restaurant_id, item.get("name") or "ITEM", taken)
                taken.add(code)
                await self.menu_repo.update_one(item["id"], {"code": code})

    async def export_menu(self, restaurant_id: str) -> bytes:
        await self._ensure_codes(restaurant_id)
        items = await self.menu_repo.find_for_restaurant(restaurant_id)
        items = [i for i in items if resolve_schedule_type(i) != ScheduleType.DATE.value]
        return build_menu_workbook(items)

    async def export_calendar(self, restaurant_id: str, from_date: str | None, to_date: str | None) -> bytes:
        await self._ensure_codes(restaurant_id)
        items = await self.menu_repo.find_calendar(restaurant_id, from_date, to_date)
        items.sort(key=lambda i: (i.get("serve_date") or "", i.get("option_number") or 0, i.get("name") or ""))
        return build_calendar_workbook(items)
