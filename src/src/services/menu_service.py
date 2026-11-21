"""Reglas de negocio del catálogo: disponibilidad por día, CRUD y validaciones."""

import re
import unicodedata
import uuid
from datetime import date, datetime

from src.core.timeutils import is_valid_hhmm, local_date_str, local_today, to_local, utc_now
from src.models.category_icons import is_valid_icon, suggest_icon
from src.models.menu import MealPeriod, ScheduleType, resolve_schedule_type
from src.models.restaurant import WEEKDAYS
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuCategoryRepository, MenuRepository
from src.repositories.order_repo import OrderRepository

EDITABLE_FIELDS = (
    "code",
    "name",
    "category",
    "description",
    "price",
    "available",
    "schedule_type",
    "day_of_week",
    "serve_date",
    "option_number",
    "meal_period",
    "prep_time_minutes",
    "ready_from",
    "order_cutoff",
    "daily_stock",
    "dine_in_only",
    "modifiers",
)


class MenuValidationError(ValueError):
    """Datos de un item de menú inválidos."""


class CategoryConflictError(ValueError):
    """La categoría ya existe en el restaurante."""


class CategoryInUseError(ValueError):
    """No se puede eliminar la categoría: todavía tiene productos."""

    def __init__(self, category: str, items: list[dict]):
        self.category = category
        self.items = items
        names = ", ".join(i["name"] for i in items)
        plural = "producto" if len(items) == 1 else "productos"
        super().__init__(f"Aún quedan {len(items)} {plural} con la categoría '{category}': {names}.")


def category_key(name: str | None) -> str:
    return (name or "").strip().lower()


def is_paused_today(item: dict, today: str | None = None) -> bool:
    """¿El item está desactivado solo por hoy (`paused_on` = fecha local de hoy)?"""
    paused_on = item.get("paused_on")
    return bool(paused_on) and paused_on == (today or local_date_str())


def is_offered_on(item: dict, day: date) -> bool:
    """¿El item se ofrece el día `day` (hora de Guatemala)? No evalúa hora límite ni existencias."""
    if item.get("deleted"):
        return False
    schedule = resolve_schedule_type(item)
    if schedule == ScheduleType.WEEKLY.value:
        return item.get("day_of_week") == WEEKDAYS[day.weekday()]
    if schedule == ScheduleType.DATE.value:
        return item.get("serve_date") == day.isoformat()
    return True


def is_before_cutoff(item: dict, now: datetime) -> bool:
    cutoff = item.get("order_cutoff")
    if not cutoff:
        return True
    return to_local(now).strftime("%H:%M") <= cutoff


def filter_available_today(items: list[dict], now: datetime | None = None) -> list[dict]:
    """Items que se pueden ofrecer hoy: programación del día y antes de su hora límite."""
    now = now or utc_now()
    today = local_today(now)
    return [item for item in items if is_offered_on(item, today) and is_before_cutoff(item, now)]


def _slug_prefix(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    letters = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    return (letters[:4] or "ITEM")


def _could_share_day(a: dict, b: dict) -> bool:
    sa, sb = resolve_schedule_type(a), resolve_schedule_type(b)
    if ScheduleType.ALWAYS.value in (sa, sb):
        return True
    if sa == sb == ScheduleType.WEEKLY.value:
        return a.get("day_of_week") == b.get("day_of_week")
    if sa == sb == ScheduleType.DATE.value:
        return a.get("serve_date") == b.get("serve_date")
    weekly, dated = (a, b) if sa == ScheduleType.WEEKLY.value else (b, a)
    serve = date.fromisoformat(dated["serve_date"])
    return weekly.get("day_of_week") == WEEKDAYS[serve.weekday()]


def find_name_conflict(candidate: dict, others: list[dict]) -> dict | None:
    """Otro item con el mismo nombre que se podría ofrecer el mismo día (el agente pide por nombre)."""
    name = (candidate.get("name") or "").strip().lower()
    for other in others:
        if other.get("deleted") or other.get("id") == candidate.get("id"):
            continue
        if (other.get("name") or "").strip().lower() == name and _could_share_day(candidate, other):
            return other
    return None


def normalize_modifiers(modifiers: list | None) -> list[dict]:
    result = []
    for raw in modifiers or []:
        if isinstance(raw, str):
            raw = {"name": raw}
        name = (raw.get("name") or "").strip()
        if not name:
            raise MenuValidationError("Cada modificador necesita un nombre.")
        price = float(raw.get("price") or 0)
        if price < 0:
            raise MenuValidationError(f"El precio del modificador '{name}' no puede ser negativo.")
        result.append(
            {
                "id": raw.get("id") or uuid.uuid4().hex[:8],
                "group": (raw.get("group") or None),
                "name": name,
                "price": round(price, 2),
            }
        )
    return result


def normalize_item(data: dict) -> dict:
    """Valida y completa los campos de un item (sin restaurant_id/código)."""
    item = dict(data)
    name = (item.get("name") or "").strip()
    category = (item.get("category") or "").strip()
    if not name:
        raise MenuValidationError("El nombre es obligatorio.")
    if not category:
        raise MenuValidationError("La categoría es obligatoria.")
    item["name"], item["category"] = name, category

    try:
        item["price"] = round(float(item.get("price")), 2)
    except (TypeError, ValueError):
        raise MenuValidationError("El precio es obligatorio y debe ser numérico.") from None
    if item["price"] < 0:
        raise MenuValidationError("El precio no puede ser negativo.")

    prep = item.get("prep_time_minutes")
    item["prep_time_minutes"] = float(prep) if prep not in (None, "") else 5.0
    if item["prep_time_minutes"] < 0:
        raise MenuValidationError("El tiempo de preparación no puede ser negativo.")

    schedule = item.get("schedule_type") or resolve_schedule_type(item)
    if schedule not in {s.value for s in ScheduleType}:
        raise MenuValidationError("schedule_type debe ser always, weekly o date.")
    item["schedule_type"] = schedule
    if schedule == ScheduleType.WEEKLY.value:
        if item.get("day_of_week") not in WEEKDAYS:
            raise MenuValidationError("day_of_week es obligatorio para items semanales (monday..sunday).")
        item["serve_date"] = None
    elif schedule == ScheduleType.DATE.value:
        try:
            item["serve_date"] = date.fromisoformat(str(item.get("serve_date"))).isoformat()
        except ValueError:
            raise MenuValidationError("serve_date es obligatorio para items de calendario (YYYY-MM-DD).") from None
        item["day_of_week"] = WEEKDAYS[date.fromisoformat(item["serve_date"]).weekday()]
    else:
        item["day_of_week"] = None
        item["serve_date"] = None

    for field in ("ready_from", "order_cutoff"):
        value = item.get(field)
        if value in (None, ""):
            item[field] = None
        elif not is_valid_hhmm(value):
            raise MenuValidationError(f"{field} debe tener formato HH:MM.")

    stock = item.get("daily_stock")
    if stock in (None, ""):
        item["daily_stock"] = None
    else:
        item["daily_stock"] = int(stock)
        if item["daily_stock"] < 1:
            raise MenuValidationError("daily_stock debe ser mayor a 0 o quedar vacío.")

    option = item.get("option_number")
    item["option_number"] = int(option) if option not in (None, "") else None

    period = item.get("meal_period")
    if period in (None, ""):
        item["meal_period"] = None
    elif period not in {p.value for p in MealPeriod}:
        raise MenuValidationError("meal_period debe ser desayuno, almuerzo, cena o vacío.")

    item["available"] = bool(item.get("available", True))
    item["dine_in_only"] = bool(item.get("dine_in_only", False))
    item["description"] = (item.get("description") or None)
    item["modifiers"] = normalize_modifiers(item.get("modifiers"))
    item["is_daily_special"] = schedule != ScheduleType.ALWAYS.value
    return item


class MenuService:
    def __init__(
        self,
        repo: MenuRepository,
        order_repo: OrderRepository | None = None,
        category_repo: MenuCategoryRepository | None = None,
        kds_repo: KdsUserRepository | None = None,
    ):
        self.repo = repo
        self.order_repo = order_repo
        self.category_repo = category_repo
        self.kds_repo = kds_repo

    # --- Público (agente) ---------------------------------------------------------

    async def list_items(self, restaurant_id: str | None = None) -> list[dict]:
        """Menú ofrecible hoy. Los items agotados o desactivados por hoy salen con `available = false`."""
        filter_ = {"deleted": {"$ne": True}}
        if restaurant_id:
            filter_["restaurant_id"] = restaurant_id
        items = filter_available_today(await self.repo.find_all(filter_))
        return await self.annotate_stock(items)

    async def annotate_stock(self, items: list[dict]) -> list[dict]:
        """Disponibilidad efectiva de hoy: existencias del día y desactivación diaria (`paused_on`)."""
        today = local_today().isoformat()
        sold_by_restaurant: dict[str, dict[str, int]] = {}
        result = []
        for item in items:
            item = dict(item)
            if is_paused_today(item, today):
                item["available"] = False
            if self.order_repo is not None and item.get("daily_stock"):
                rid = item["restaurant_id"]
                if rid not in sold_by_restaurant:
                    sold_by_restaurant[rid] = await self.order_repo.sold_quantities_on(rid, today)
                remaining = max(0, item["daily_stock"] - sold_by_restaurant[rid].get(item["id"], 0))
                item["remaining_stock"] = remaining
                if remaining == 0:
                    item["available"] = False
            result.append(item)
        return result

    # --- Administración -------------------------------------------------------------

    async def admin_list(
        self,
        restaurant_id: str,
        category: str | None = None,
        schedule_type: str | None = None,
        on_date: date | None = None,
        include_unavailable: bool = True,
    ) -> list[dict]:
        extra: dict = {}
        if category:
            extra["category"] = category
        if not include_unavailable:
            extra["available"] = True
        items = await self.repo.find_for_restaurant(restaurant_id, extra)
        if not include_unavailable:
            today = local_date_str()
            items = [i for i in items if not is_paused_today(i, today)]
        icon_map = await self.category_icon_map(restaurant_id)
        for item in items:
            item["schedule_type"] = resolve_schedule_type(item)
            item["category_icon"] = self.icon_for(item.get("category"), icon_map)
        if schedule_type:
            items = [i for i in items if i["schedule_type"] == schedule_type]
        if on_date is not None:
            items = [i for i in items if is_offered_on(i, on_date)]
        return items

    async def get_item(self, restaurant_id: str, item_id: str) -> dict | None:
        item = await self.repo.find_by_id(item_id)
        if item is None or item.get("restaurant_id") != restaurant_id or item.get("deleted"):
            return None
        item["schedule_type"] = resolve_schedule_type(item)
        return item

    async def generate_code(self, restaurant_id: str, name: str, taken: set[str] | None = None) -> str:
        prefix = _slug_prefix(name)
        taken = taken or set()
        n = 1
        while True:
            code = f"{prefix}-{n:02d}"
            if code not in taken and await self.repo.find_by_code(restaurant_id, code) is None:
                return code
            n += 1

    async def create_item(self, restaurant_id: str, data: dict) -> dict:
        item = normalize_item({k: data.get(k) for k in EDITABLE_FIELDS if k in data})
        existing = await self.repo.find_for_restaurant(restaurant_id)
        conflict = find_name_conflict(item, existing)
        if conflict:
            raise MenuValidationError(
                f"Ya existe '{conflict['name']}' ofrecido el mismo día (código {conflict.get('code')}). Usa otro nombre."
            )
        code = (data.get("code") or "").strip()
        if code:
            if await self.repo.find_by_code(restaurant_id, code):
                raise MenuValidationError(f"El código '{code}' ya existe en este restaurante.")
        else:
            code = await self.generate_code(restaurant_id, item["name"])
        now = utc_now()
        document = item | {
            "restaurant_id": restaurant_id,
            "code": code,
            "deleted": False,
            "deleted_at": None,
            "created_at": now,
            "updated_at": now,
        }
        return await self.repo.insert_one(document)

    async def update_item(self, restaurant_id: str, item_id: str, data: dict) -> dict | None:
        current = await self.get_item(restaurant_id, item_id)
        if current is None:
            return None
        merged = {k: current.get(k) for k in EDITABLE_FIELDS} | {k: v for k, v in data.items() if k in EDITABLE_FIELDS}
        # Al cambiar de programación, limpiar campos del tipo anterior.
        if "schedule_type" in data:
            if data["schedule_type"] != ScheduleType.WEEKLY.value and "day_of_week" not in data:
                merged["day_of_week"] = None
            if data["schedule_type"] != ScheduleType.DATE.value and "serve_date" not in data:
                merged["serve_date"] = None
        item = normalize_item(merged)
        item["id"] = item_id
        existing = await self.repo.find_for_restaurant(restaurant_id)
        conflict = find_name_conflict(item, existing)
        if conflict:
            raise MenuValidationError(
                f"Ya existe '{conflict['name']}' ofrecido el mismo día (código {conflict.get('code')}). Usa otro nombre."
            )
        code = (merged.get("code") or "").strip() or current.get("code")
        if code != current.get("code"):
            other = await self.repo.find_by_code(restaurant_id, code)
            if other and other["id"] != item_id:
                raise MenuValidationError(f"El código '{code}' ya existe en este restaurante.")
        item.pop("id")
        changes = item | {"code": code, "updated_at": utc_now()}
        if data.get("available") is True:
            changes["paused_on"] = None  # activar a mano también quita la desactivación de hoy
        return await self.repo.update_one(item_id, changes)

    # --- Disponibilidad (KDS) ---------------------------------------------------------

    async def set_availability(self, restaurant_id: str, item_id: str, available: bool, mode: str | None) -> dict | None:
        """Activa o desactiva un item. `mode`: `permanent` (hasta activarlo a mano) o `daily` (solo hoy)."""
        current = await self.get_item(restaurant_id, item_id)
        if current is None:
            return None
        if available:
            changes = {"available": True, "paused_on": None}
        elif mode == "daily":
            changes = {"available": True, "paused_on": local_date_str()}
        elif mode == "permanent":
            changes = {"available": False, "paused_on": None}
        else:
            raise MenuValidationError("Al desactivar se debe indicar mode: permanent o daily.")
        updated = await self.repo.update_one(item_id, changes | {"updated_at": utc_now()})
        updated["schedule_type"] = resolve_schedule_type(updated)
        return updated

    async def pause_items_today(self, restaurant_id: str, item_ids: list[str]) -> None:
        await self.repo.pause_many(restaurant_id, item_ids, local_date_str(), utc_now())

    # --- Categorías ---------------------------------------------------------------------

    async def list_categories(self, restaurant_id: str) -> list[str]:
        """Categorías creadas desde el panel + las que ya usa algún producto."""
        by_key: dict[str, str] = {}
        if self.category_repo is not None:
            for category in await self.category_repo.find_for_restaurant(restaurant_id):
                by_key.setdefault(category["name_key"], category["name"])
        for item in await self.repo.find_for_restaurant(restaurant_id):
            name = (item.get("category") or "").strip()
            if name:
                by_key.setdefault(category_key(name), name)
        return sorted(by_key.values(), key=str.lower)

    async def list_category_details(self, restaurant_id: str) -> list[dict]:
        """`[{name, icon, products_count}]`. Una categoría sin icono guardado muestra el sugerido por su nombre."""
        stored: dict[str, dict] = {}
        if self.category_repo is not None:
            for category in await self.category_repo.find_for_restaurant(restaurant_id):
                stored[category["name_key"]] = category
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in await self.repo.find_for_restaurant(restaurant_id):
            name = (item.get("category") or "").strip()
            if name:
                key = category_key(name)
                counts[key] = counts.get(key, 0) + 1
                names.setdefault(key, name)
        result = []
        for key in stored.keys() | names.keys():
            name = stored[key]["name"] if key in stored else names[key]
            icon = (stored.get(key) or {}).get("icon")
            result.append(
                {
                    "name": name,
                    "icon": icon if is_valid_icon(icon) else suggest_icon(name),
                    "products_count": counts.get(key, 0),
                }
            )
        return sorted(result, key=lambda c: c["name"].lower())

    async def category_icon_map(self, restaurant_id: str) -> dict[str, str]:
        """`name_key -> icon` de las categorías con icono guardado (para estampar en los pedidos)."""
        if self.category_repo is None:
            return {}
        return {
            c["name_key"]: c["icon"]
            for c in await self.category_repo.find_for_restaurant(restaurant_id)
            if is_valid_icon(c.get("icon"))
        }

    @staticmethod
    def icon_for(category: str | None, icon_map: dict[str, str]) -> str:
        return icon_map.get(category_key(category)) or suggest_icon(category)

    @staticmethod
    def _validate_icon(icon: str | None) -> str | None:
        if icon is None:
            return None
        if not is_valid_icon(icon):
            raise MenuValidationError(f"El icono '{icon}' no existe.")
        return icon

    async def create_category(self, restaurant_id: str, name: str, icon: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise MenuValidationError("El nombre de la categoría es obligatorio.")
        icon = self._validate_icon(icon) or suggest_icon(name)
        key = category_key(name)
        existing = next((c for c in await self.list_categories(restaurant_id) if category_key(c) == key), None)
        if existing is not None:
            raise CategoryConflictError(f"La categoría '{existing}' ya existe.")
        await self.category_repo.insert_one(
            {"restaurant_id": restaurant_id, "name": name, "name_key": key, "icon": icon, "created_at": utc_now()}
        )
        return {"name": name, "icon": icon}

    async def update_category(
        self, restaurant_id: str, name: str, new_name: str | None = None, icon: str | None = None
    ) -> dict | None:
        """Cambia el icono y/o el nombre de una categoría. `None` si no existe.

        Renombrar actualiza en cascada los productos del restaurante y los filtros de menú de sus
        estaciones KDS (que comparan por nombre). Los pedidos ya creados conservan el nombre con el
        que se hicieron (es historial)."""
        key = category_key(name)
        details = await self.list_category_details(restaurant_id)
        current = next((c for c in details if category_key(c["name"]) == key), None)
        if current is None:
            return None

        icon = self._validate_icon(icon)
        final_name = current["name"]
        if new_name is not None:
            final_name = new_name.strip()
            if not final_name:
                raise MenuValidationError("El nombre de la categoría es obligatorio.")
            if category_key(final_name) != key:
                clash = next(
                    (c for c in await self.list_categories(restaurant_id) if category_key(c) == category_key(final_name)),
                    None,
                )
                if clash is not None:
                    raise CategoryConflictError(f"La categoría '{clash}' ya existe.")

        now = utc_now()
        # Si se renombra y no se pidió icono, se conserva el que ya mostraba la categoría.
        data = {
            "restaurant_id": restaurant_id,
            "name": final_name,
            "name_key": category_key(final_name),
            "icon": icon or current["icon"],
        }
        await self.category_repo.upsert_by_key(restaurant_id, key, data, now)

        if final_name != current["name"]:
            ids = [
                i["id"]
                for i in await self.repo.find_for_restaurant(restaurant_id)
                if category_key(i.get("category")) == key
            ]
            await self.repo.set_category_many(restaurant_id, ids, final_name, now)
            await self._rename_in_kds_filters(restaurant_id, key, final_name)
        await self._refresh_active_orders(restaurant_id, key, final_name, data["icon"])
        return {"name": final_name, "icon": data["icon"]}

    async def _refresh_active_orders(self, restaurant_id: str, old_key: str, name: str, icon: str) -> None:
        """Los pedidos EN CURSO (los que la cocina ve) toman el nombre e icono nuevos de la categoría;
        los ya cerrados conservan el snapshot con el que se hicieron."""
        if self.order_repo is None:
            return
        for order in await self.order_repo.find_active(restaurant_id):
            items = order.get("items") or []
            touched = False
            for item in items:
                if category_key(item.get("category")) == old_key:
                    item["category"], item["category_icon"] = name, icon
                    touched = True
            if touched:
                await self.order_repo.update_fields(order["id"], {"items": items})

    async def _rename_in_kds_filters(self, restaurant_id: str, old_key: str, new_name: str) -> None:
        if self.kds_repo is None:
            return
        for station in await self.kds_repo.find_for_restaurant(restaurant_id):
            categories = (station.get("menu_filter") or {}).get("categories") or []
            if any(category_key(c) == old_key for c in categories):
                renamed = [new_name if category_key(c) == old_key else c for c in categories]
                await self.kds_repo.set_menu_filter_categories(station["id"], renamed)

    async def delete_category(self, restaurant_id: str, name: str) -> bool:
        """Elimina la categoría si ningún producto la usa. `False` si no existe."""
        key = category_key(name)
        in_use = [
            {"id": i["id"], "code": i.get("code"), "name": i["name"]}
            for i in await self.repo.find_for_restaurant(restaurant_id)
            if category_key(i.get("category")) == key
        ]
        if in_use:
            raise CategoryInUseError(name.strip(), in_use)
        return await self.category_repo.delete_by_key(restaurant_id, key) > 0

    async def delete_item(self, restaurant_id: str, item_id: str) -> bool:
        current = await self.get_item(restaurant_id, item_id)
        if current is None:
            return False
        await self.repo.soft_delete_many([item_id], utc_now())
        return True

    async def create_item_from_schema(self, data) -> dict:
        """Compatibilidad con POST /api/menus/ (el restaurante viene en el body)."""
        payload = data.model_dump()
        return await self.create_item(payload.pop("restaurant_id"), payload)
