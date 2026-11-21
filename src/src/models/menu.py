"""
Forma del documento de Mongo para la colección `menu_items`.

Tipos de programación (`schedule_type`):

- `always`: se ofrece todos los días que abre el restaurante.
- `weekly`: se ofrece solo el `day_of_week` indicado (ej. almuerzo del día fijo por día).
- `date`: se ofrece solo en `serve_date` (menú de calendario, se carga con la
  plantilla de calendario).

Los documentos antiguos sin `schedule_type` se interpretan con
`resolve_schedule_type`: `is_daily_special` + `day_of_week` = `weekly`.

Disponibilidad: `available = false` es una desactivación permanente (hasta
que alguien lo active a mano). `paused_on = "YYYY-MM-DD"` es una desactivación
solo por ese día (hora de Guatemala): al día siguiente el valor ya no aplica
y el producto vuelve a ofrecerse sin que nadie tenga que reactivarlo.

Categorías: `menu_categories` guarda las categorías creadas desde el panel
(`{restaurant_id, name, name_key, created_at}`); un producto también puede
traer una categoría que no esté registrada ahí (ej. importación de Excel).

`modifiers`: opciones del producto con precio extra (ej. "Sin tomate" Q0.00,
"Doble queso" Q5.00). El pedido guarda un snapshot con nombre y precio.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

COLLECTION_NAME = "menu_items"
CATEGORIES_COLLECTION_NAME = "menu_categories"


class MealPeriod(str, Enum):
    BREAKFAST = "desayuno"
    LUNCH = "almuerzo"
    DINNER = "cena"


class ScheduleType(str, Enum):
    ALWAYS = "always"
    WEEKLY = "weekly"
    DATE = "date"


# Única fuente de verdad para las ventanas horarias de cada período.
MEAL_PERIOD_WINDOWS: dict[MealPeriod, tuple[str, str]] = {
    MealPeriod.BREAKFAST: ("06:00", "11:00"),
    MealPeriod.LUNCH: ("11:00", "17:00"),
    MealPeriod.DINNER: ("17:00", "20:00"),
}

DEFAULT_CALENDAR_NAME = "Almuerzo del día"
DEFAULT_CALENDAR_CATEGORY = "almuerzos del día"


def resolve_schedule_type(item: dict) -> str:
    value = item.get("schedule_type")
    if value in {s.value for s in ScheduleType}:
        return value
    if item.get("serve_date"):
        return ScheduleType.DATE.value
    if item.get("is_daily_special") and item.get("day_of_week"):
        return ScheduleType.WEEKLY.value
    return ScheduleType.ALWAYS.value


@dataclass
class MenuModifier:
    id: str
    name: str
    price: float = 0.0
    group: str | None = None


@dataclass
class MenuItemDocument:
    """
    Ejemplo (producto fijo):
    {
        "_id": ObjectId("..."),
        "restaurant_id": "665f1c...",
        "code": "HAMB-01",
        "name": "Hamburguesa",
        "category": "comida",
        "description": null,
        "price": 25.0,
        "available": true,
        "schedule_type": "always",
        "day_of_week": null,
        "serve_date": null,
        "option_number": null,
        "meal_period": null,
        "prep_time_minutes": 8,
        "ready_from": null,
        "order_cutoff": null,
        "daily_stock": null,
        "dine_in_only": false,
        "modifiers": [
            {"id": "a1b2c3", "group": "Quitar", "name": "Sin tomate", "price": 0.0},
            {"id": "d4e5f6", "group": "Extras", "name": "Doble queso", "price": 5.0}
        ],
        "is_daily_special": false,
        "deleted": false,
        "deleted_at": null,
        "created_at": ISODate("..."),
        "updated_at": ISODate("...")
    }

    Ejemplo (calendario): `schedule_type = "date"`, `serve_date = "2026-09-25"`,
    `option_number = 1`, `name = "Almuerzo del día - Opción 1"`,
    `category = "almuerzos del día"`, `ready_from = "11:30"`.
    """

    restaurant_id: str
    code: str
    name: str
    category: str
    price: float
    available: bool = True
    paused_on: str | None = None
    schedule_type: ScheduleType = ScheduleType.ALWAYS
    day_of_week: str | None = None
    serve_date: str | None = None
    option_number: int | None = None
    meal_period: MealPeriod | None = None
    description: str | None = None
    prep_time_minutes: float = 5.0
    ready_from: str | None = None
    order_cutoff: str | None = None
    daily_stock: int | None = None
    dine_in_only: bool = False
    modifiers: list[dict] = field(default_factory=list)
    is_daily_special: bool = False
    deleted: bool = False
    deleted_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
