"""
¿Se le puede pedir a un restaurante AHORA? Única fuente de verdad para:

- `GET /api/restaurants/` (campo `status` que lee el chatbot),
- `GET /api/menus/` (cada producto trae el estado de su restaurante),
- `POST /api/orders/estimate` y `POST /api/orders/` (rechazan con 409).

Regla de oro: no se hacen pedidos de un día a otro. Solo se pide mientras el
restaurante está abierto, para HOY y para estar listo antes del cierre.

Códigos de error (van en `detail.code` de la respuesta 409):
- `kitchen_closed`: fuera del horario de atención (o cerrado todo el día).
- `kitchen_closing`: ya pasó la hora del último pedido (`LAST_ORDER_MINUTES_BEFORE_CLOSE`,
  30 min antes del cierre por defecto): la cocina ya no recibe pedidos nuevos hoy.
- `kitchen_out_of_service`: dentro de horario, pero todas las estaciones KDS
  están en pausa (o el restaurante está desactivado).
- `outside_opening_hours`: la hora pedida es de otro día o después del cierre,
  o el pedido ya no alcanza a estar listo antes de que cierre.
"""

from datetime import date, datetime, timedelta

from src.configuration import settings
from src.core.timeutils import GUATEMALA_TZ, format_local_12h, to_local, utc_now
from src.models.restaurant import opening_window_for_date

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

CODE_KITCHEN_CLOSED = "kitchen_closed"
CODE_KITCHEN_CLOSING = "kitchen_closing"
CODE_OUT_OF_SERVICE = "kitchen_out_of_service"
CODE_OUTSIDE_HOURS = "outside_opening_hours"

_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_NEXT_OPENING_LOOKAHEAD_DAYS = 7


class RestaurantUnavailableError(ValueError):
    """El restaurante no puede recibir este pedido. `to_detail()` es el body del 409."""

    def __init__(self, code: str, message: str, availability: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.availability = availability or {}

    def to_detail(self) -> dict:
        keys = ("status", "opens_at", "closes_at", "last_order_at", "next_opening")
        return {"code": self.code, "message": self.message, **{k: self.availability.get(k) for k in keys}}


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "hoy"
    if day == today + timedelta(days=1):
        return "mañana"
    return f"el {_WEEKDAYS_ES[day.weekday()]}"


def _next_opening(restaurant: dict, now: datetime) -> tuple[date, datetime] | None:
    """Próxima apertura (fecha local, instante UTC) posterior a `now`."""
    today = to_local(now).date()
    for offset in range(_NEXT_OPENING_LOOKAHEAD_DAYS + 1):
        day = today + timedelta(days=offset)
        window = opening_window_for_date(restaurant, day)
        if window is not None and window[0] > now:
            return day, window[0]
    return None


def restaurant_availability(restaurant: dict, stations: list[dict] | None = None, now: datetime | None = None) -> dict:
    """Estado del restaurante en `now` (UTC). `stations` = estaciones KDS del restaurante
    (`None` o `[]` = no se consideran, igual que el reparto de pedidos)."""
    now = now or utc_now()
    today = to_local(now).date()
    window = opening_window_for_date(restaurant, today)
    opens_at = format_local_12h(window[0]) if window else None
    closes_at = format_local_12h(window[1]) if window else None
    in_hours = window is not None and window[0] <= now < window[1]
    last_order_utc = None
    if window is not None:
        cutoff = timedelta(minutes=max(0, settings.payment.last_order_minutes_before_close))
        last_order_utc = max(window[0], window[1] - cutoff)
    last_order_at = format_local_12h(last_order_utc)

    next_opening = None
    nxt = _next_opening(restaurant, now)
    if nxt is not None:
        next_opening = f"{_day_label(nxt[0], today)} a las {format_local_12h(nxt[1])}"

    name = restaurant.get("name", "El restaurante")
    if not restaurant.get("active", True):
        code, message = CODE_OUT_OF_SERVICE, f"{name} no está recibiendo pedidos por el momento."
    elif not in_hours:
        code = CODE_KITCHEN_CLOSED
        message = f"{name} está cerrado en este momento."
        if next_opening:
            message += f" Abre {next_opening}."
    elif now >= last_order_utc:
        code = CODE_KITCHEN_CLOSING
        message = (
            f"{name} ya no está recibiendo pedidos por hoy: cierra a las {closes_at} "
            f"y el último pedido es a las {last_order_at}."
        )
        if next_opening:
            message += f" Abre {next_opening}."
    elif stations and not any(s.get("active", True) for s in stations):
        code = CODE_OUT_OF_SERVICE
        message = f"La cocina de {name} está en pausa y no está recibiendo pedidos por el momento."
    else:
        code = None
        message = f"{name} está abierto hasta las {closes_at} (último pedido a las {last_order_at})."

    return {
        "status": STATUS_OPEN if code is None else STATUS_CLOSED,
        "is_open_now": code is None,
        "closed_reason": code,
        "opens_at": opens_at,
        "closes_at": closes_at,
        "last_order_at": last_order_at,
        "next_opening": next_opening if code is not None else None,
        "status_message": message,
        # Interno (no se serializa en los schemas públicos).
        "_close_utc": window[1] if window else None,
        "_today": today,
    }


def ensure_can_order(
    restaurant: dict,
    stations: list[dict] | None,
    now: datetime,
    requested_ready_time_utc: datetime | None = None,
) -> dict:
    """Lanza RestaurantUnavailableError si no se puede pedir ahora (o para esa hora)."""
    availability = restaurant_availability(restaurant, stations, now)
    if availability["closed_reason"]:
        raise RestaurantUnavailableError(availability["closed_reason"], availability["status_message"], availability)
    if requested_ready_time_utc is not None:
        name = restaurant.get("name", "El restaurante")
        requested_local = requested_ready_time_utc.astimezone(GUATEMALA_TZ)
        if requested_local.date() != availability["_today"]:
            raise RestaurantUnavailableError(
                CODE_OUTSIDE_HOURS,
                "Solo se pueden hacer pedidos para hoy: no se aceptan pedidos de un día para otro.",
                availability,
            )
        if requested_ready_time_utc > availability["_close_utc"]:
            raise RestaurantUnavailableError(
                CODE_OUTSIDE_HOURS,
                f"{name} cierra hoy a las {availability['closes_at']}: elige una hora antes del cierre.",
                availability,
            )
    return availability


def ensure_ready_today(restaurant: dict, availability: dict, prep_start_utc: datetime, ready_at_utc: datetime) -> None:
    """El horario calculado (cola + preparación) tiene que caer hoy y antes del cierre."""
    close_utc = availability.get("_close_utc")
    if to_local(prep_start_utc).date() != availability["_today"] or close_utc is None or ready_at_utc > close_utc:
        name = restaurant.get("name", "El restaurante")
        raise RestaurantUnavailableError(
            CODE_OUTSIDE_HOURS,
            f"El pedido ya no alcanza a estar listo antes de que {name} cierre hoy "
            f"({availability.get('closes_at')}). No se aceptan pedidos para otro día.",
            availability,
        )


def public_availability(availability: dict) -> dict:
    return {k: v for k, v in availability.items() if not k.startswith("_")}
