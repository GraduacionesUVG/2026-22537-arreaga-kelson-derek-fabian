"""Schemas públicos del catálogo (GET /api/menus/, usado por el agente)."""

from typing import Literal

from pydantic import BaseModel, Field

from src.models.menu import MealPeriod


class MenuModifierPublic(BaseModel):
    name: str
    price: float = 0.0
    group: str | None = None


class MenuItemBase(BaseModel):
    restaurant_id: str
    name: str
    category: str
    price: float
    available: bool = True
    meal_period: MealPeriod | None = None
    is_daily_special: bool = False
    day_of_week: str | None = None
    description: str | None = None
    prep_time_minutes: float = 5.0
    dine_in_only: bool = False


class MenuItemCreate(MenuItemBase):
    schedule_type: str | None = None
    serve_date: str | None = None
    option_number: int | None = None
    ready_from: str | None = None
    order_cutoff: str | None = None
    daily_stock: int | None = None
    modifiers: list[MenuModifierPublic] = Field(default_factory=list)


class MenuItemResponse(MenuItemBase):
    id: str
    code: str | None = None
    schedule_type: str | None = None
    serve_date: str | None = None
    option_number: int | None = None
    ready_from: str | None = Field(default=None, description="Listo aprox. desde esta hora (HH:MM).")
    order_cutoff: str | None = Field(default=None, description="Hora límite para pedirlo hoy (HH:MM).")
    remaining_stock: int | None = None
    modifiers: list[MenuModifierPublic] = Field(default_factory=list)
    # Estado del restaurante AHORA (mismo cálculo que GET /api/restaurants/): con
    # "closed" el producto se puede mostrar, pero no pedir.
    restaurant_status: Literal["open", "closed"] | None = Field(
        default=None, description="open = el restaurante recibe pedidos ahora."
    )
    restaurant_closed_reason: str | None = Field(
        default=None,
        description="kitchen_closed (fuera de horario) | kitchen_closing (pasó la hora del último pedido) | "
        "kitchen_out_of_service (cocina en pausa).",
    )
    restaurant_status_message: str | None = None
