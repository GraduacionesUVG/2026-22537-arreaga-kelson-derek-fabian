"""Schemas públicos de restaurantes (GET /api/restaurants/, usado por el agente)."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from src.models.restaurant import GUATEMALA_TZ, weekday_name


class DayHours(BaseModel):
    open: str | None = None  # "HH:MM", 24h
    close: str | None = None  # "HH:MM", 24h
    closed: bool = False


class RestaurantBase(BaseModel):
    name: str
    location: str | None = None
    active: bool = True
    opening_hours: dict[str, DayHours | None] = {}
    service_fee: float = Field(
        default=0.0, ge=0, description="Tarifa de servicio (Q) que se suma al total de cada pedido."
    )


class RestaurantCreate(RestaurantBase):
    pass


class RestaurantResponse(RestaurantBase):
    id: str
    # Disponibilidad AHORA (src/services/restaurant_availability.py). El chatbot debe
    # revisar `status` antes de ofrecer pedir: con "closed" no se puede pedir.
    status: Literal["open", "closed"] = Field(default="open", description="open = se puede pedir ahora.")
    is_open_now: bool = True
    closed_reason: str | None = Field(
        default=None,
        description="kitchen_closed (fuera de horario) | kitchen_closing (pasó la hora del último pedido) | "
        "kitchen_out_of_service (cocina en pausa).",
    )
    opens_at: str | None = Field(default=None, description="Hora de apertura de hoy, hora de Guatemala (\"8:00 am\").")
    closes_at: str | None = Field(default=None, description="Hora de cierre de hoy, hora de Guatemala (\"8:00 pm\").")
    last_order_at: str | None = Field(
        default=None, description="Hora límite para pedir hoy (cierre - LAST_ORDER_MINUTES_BEFORE_CLOSE, \"7:30 pm\")."
    )
    next_opening: str | None = Field(default=None, description="Si está cerrado: cuándo vuelve a abrir.")
    status_message: str | None = Field(default=None, description="Estado listo para decirle al estudiante.")

    @computed_field
    @property
    def today(self) -> DayHours | None:
        """Horario de HOY según la hora local de Guatemala, `None` = cerrado hoy."""
        local_now = datetime.now(timezone.utc).astimezone(GUATEMALA_TZ)
        hours = self.opening_hours.get(weekday_name(local_now))
        if hours is None or hours.closed or not hours.open:
            return None
        return hours
