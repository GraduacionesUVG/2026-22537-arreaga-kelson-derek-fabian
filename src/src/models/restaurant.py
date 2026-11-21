"""
Forma del documento de Mongo para la colección `restaurants`, además de las
funciones que interpretan `opening_hours` contra un instante dado (usadas
por el algoritmo de ETC en `src/services/order_service.py` para no agendar
pedidos fuera del horario del restaurante).

TODO (Módulo D - Catálogos): KDS asociado, métodos de pago habilitados, etc.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

COLLECTION_NAME = "restaurants"

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Todos los restaurantes operan en Guatemala (UTC-6, sin horario de verano) --
# única fuente de verdad para "qué hora/día es hoy" de cara al usuario, aunque
# el backend guarde todo en UTC internamente (ver src/models/order.py).
GUATEMALA_TZ = ZoneInfo("America/Guatemala")

# Cuántos días hacia adelante se busca una ventana de apertura antes de darse
# por vencido (ver next_available_start) -- cubre el caso de un restaurante
# cerrado toda una semana sin quedar en un loop infinito.
MAX_LOOKAHEAD_DAYS = 14


def weekday_name(dt: datetime) -> str:
    """`dt.weekday()` (0=lunes) -> nombre en WEEKDAYS. Única fuente de verdad para
    "qué día es hoy" en todo el proyecto (opening_hours, almuerzo del día, ETC).

    Nota: si `dt` no está ya en hora local de Guatemala (ej. viene en UTC), el
    resultado puede ser el día equivocado cerca de la medianoche -- convertir
    primero con `dt.astimezone(GUATEMALA_TZ)`.
    """
    return WEEKDAYS[dt.weekday()]


def parse_hhmm(value: str) -> time:
    """`"09:00"` -> `time(9, 0)`. Formato usado en `opening_hours` (ver DayHours)."""
    hour_str, minute_str = value.split(":", 1)
    return time(int(hour_str), int(minute_str))


def opening_window_for_date(restaurant: dict, local_date: date) -> tuple[datetime, datetime] | None:
    """Ventana de apertura de `restaurant` para `local_date` (una fecha en hora
    de Guatemala), devuelta como instantes UTC `(open_utc, close_utc)`.
    `None` si ese día está cerrado (`opening_hours[dia] = null`, o el
    restaurante no tiene ese día configurado).

    Asume que ningún restaurante cierra después de medianoche (todas las
    ventanas configuradas hoy son del mismo día, ej. 09:00-20:00) -- si algún
    día un restaurante abre de noche y cierra pasada la medianoche, esta
    función necesita manejar `close_time < open_time` como "cierra al día
    siguiente".
    """
    hours = (restaurant.get("opening_hours") or {}).get(WEEKDAYS[local_date.weekday()])
    if not hours or hours.get("closed") or not hours.get("open") or not hours.get("close"):
        return None
    open_time = parse_hhmm(hours["open"])
    close_time = parse_hhmm(hours["close"])
    open_local = datetime.combine(local_date, open_time, tzinfo=GUATEMALA_TZ)
    close_local = datetime.combine(local_date, close_time, tzinfo=GUATEMALA_TZ)
    return open_local.astimezone(timezone.utc), close_local.astimezone(timezone.utc)


# Tarifa de servicio (Q) que se le suma al cliente en CADA pedido, configurable por
# restaurante (`restaurants.service_fee`). Es un monto fijo por pedido, no un porcentaje
# ni por producto. Los restaurantes sin el campo no cobran tarifa.
DEFAULT_SERVICE_FEE = 0.0
MAX_SERVICE_FEE = 1000.0


def service_fee_for(restaurant: dict | None) -> float:
    """Tarifa de servicio vigente del restaurante, en quetzales (siempre >= 0, 2 decimales)."""
    try:
        fee = float((restaurant or {}).get("service_fee") or DEFAULT_SERVICE_FEE)
    except (TypeError, ValueError):
        fee = DEFAULT_SERVICE_FEE
    return round(max(0.0, fee), 2)


DEFAULT_OPENING_HOURS = {
    **{day: {"closed": False, "open": "08:00", "close": "20:00"} for day in WEEKDAYS[:6]},
    "sunday": {"closed": True, "open": None, "close": None},
}


def normalize_opening_hours(opening_hours: dict | None) -> dict:
    """Siempre las 7 llaves de WEEKDAYS con `{closed, open, close}`.

    Acepta el formato anterior (`null` = cerrado, `{open, close}` = abierto).
    """
    opening_hours = opening_hours or {}
    result = {}
    for day in WEEKDAYS:
        hours = opening_hours.get(day)
        if not hours or hours.get("closed") or not hours.get("open") or not hours.get("close"):
            result[day] = {"closed": True, "open": None, "close": None}
        else:
            result[day] = {"closed": False, "open": hours["open"], "close": hours["close"]}
    return result


def is_open_at(restaurant: dict, at_utc: datetime) -> bool:
    """¿`restaurant` está abierto en el instante `at_utc` (datetime tz-aware, cualquier zona)?"""
    local_date = at_utc.astimezone(GUATEMALA_TZ).date()
    window = opening_window_for_date(restaurant, local_date)
    if window is None:
        return False
    open_utc, close_utc = window
    return open_utc <= at_utc < close_utc


def fits_in_opening_hours(restaurant: dict, start_utc: datetime, end_utc: datetime) -> bool:
    """¿La ventana `[start_utc, end_utc]` completa cabe dentro del horario de UN
    mismo día de `restaurant`? (no solo que `start_utc` caiga en horario --
    tiene que también terminar antes de que cierre)."""
    local_date = start_utc.astimezone(GUATEMALA_TZ).date()
    window = opening_window_for_date(restaurant, local_date)
    if window is None:
        return False
    open_utc, close_utc = window
    return open_utc <= start_utc and end_utc <= close_utc


def next_available_start(
    restaurant: dict,
    earliest_utc: datetime,
    duration_minutes: float,
    max_days_ahead: int = MAX_LOOKAHEAD_DAYS,
) -> datetime:
    """Primer instante `>= earliest_utc` en el que `restaurant` puede empezar
    algo que tarda `duration_minutes` sin pasarse de la hora de cierre de ese
    día. Recorre día por día (hora de Guatemala) hasta `max_days_ahead`.

    Usada cuando el horario "natural" (ASAP, o la hora que pidió el
    estudiante) cae fuera del horario del restaurante -- reemplaza esa hora
    por la próxima en la que el pedido sí puede prepararse y entregarse
    dentro de horario (ver OrderService._compute_schedule).
    """
    local_start_date = earliest_utc.astimezone(GUATEMALA_TZ).date()
    duration = timedelta(minutes=duration_minutes)

    for day_offset in range(max_days_ahead + 1):
        local_date = local_start_date + timedelta(days=day_offset)
        window = opening_window_for_date(restaurant, local_date)
        if window is None:
            continue
        open_utc, close_utc = window
        candidate_start = max(open_utc, earliest_utc) if day_offset == 0 else open_utc
        if candidate_start + duration <= close_utc:
            return candidate_start

    raise ValueError(
        f"'{restaurant.get('name', restaurant.get('id', '?'))}' no tiene ninguna franja horaria en los "
        f"próximos {max_days_ahead} días donde quepan {duration_minutes:.0f} minutos de preparación -- "
        "revisa su `opening_hours` configurado."
    )


@dataclass
class RestaurantDocument:
    """
    Ejemplo de documento:

    {
        "_id": ObjectId("..."),
        "name": "Cafetería Central",
        "location": "Edificio A",
        "active": true,
        "service_fee": 2.65,
        "opening_hours": {
            "monday": {"open": "07:00", "close": "20:00"},
            "tuesday": {"open": "07:00", "close": "20:00"},
            ...
            "sunday": null
        },
        "created_at": ISODate("...")
    }

    `opening_hours` tiene una llave por día de WEEKDAYS; un día en `null`
    significa cerrado. `OrderService` usa `fits_in_opening_hours` /
    `next_available_start` (arriba) para que ningún pedido quede agendado
    (`ready_at`) fuera de esta ventana, y para reagendar automáticamente al
    horario siguiente cuando la hora pedida (o la hora ASAP) cae fuera de ella.

    `service_fee` es la tarifa fija (Q) que se le suma al cliente en cada pedido
    (ver `service_fee_for`). Se copia al pedido al crearlo (snapshot): cambiarla
    no altera pedidos ya creados.
    """

    name: str
    location: str
    active: bool = True
    opening_hours: dict[str, dict[str, str] | None] = field(default_factory=dict)
    service_fee: float = DEFAULT_SERVICE_FEE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
