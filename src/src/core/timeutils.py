"""Utilidades de fecha/hora compartidas.

Todo se guarda en UTC; las fechas de negocio (qué día es hoy, a qué hora
abre un restaurante, a qué día pertenece un pedido) se calculan en hora de
Guatemala.
"""

from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import PlainSerializer

GUATEMALA_TZ = ZoneInfo("America/Guatemala")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """Datetime que ya representa UTC pero puede venir naive (ej. desde Mongo)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def to_local(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(GUATEMALA_TZ)


def local_today(now: datetime | None = None) -> date:
    return to_local(now or utc_now()).date()


def local_date_str(dt: datetime | None = None) -> str:
    return to_local(dt or utc_now()).date().isoformat()


def local_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[inicio, fin) del día `day` (hora de Guatemala) expresado en UTC."""
    start = datetime.combine(day, time(0, 0), tzinfo=GUATEMALA_TZ)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def parse_iso_date(value: str | None) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(value)


def parse_hhmm(value: str) -> time:
    hour_str, minute_str = value.split(":", 1)
    return time(int(hour_str), int(minute_str))


def is_valid_hhmm(value: str | None) -> bool:
    if not value or len(value) != 5 or value[2] != ":":
        return False
    try:
        t = parse_hhmm(value)
    except ValueError:
        return False
    return 0 <= t.hour <= 23 and 0 <= t.minute <= 59


def format_local_hhmm(dt: datetime | None) -> str:
    if dt is None:
        return "--:--"
    return to_local(dt).strftime("%H:%M")


def format_local_12h(dt: datetime | None) -> str | None:
    """Hora de Guatemala lista para leerle al cliente: "2:05 pm" (None si no hay hora)."""
    if dt is None:
        return None
    return to_local(dt).strftime("%I:%M %p").lstrip("0").lower()


# Datetime que en las RESPUESTAS de la API se serializa en hora de Guatemala
# (ej. "2026-10-13T14:05:00-06:00") en vez de UTC ("...T20:05:00Z"). Es el mismo
# instante -- cualquier cliente que parsee ISO 8601 (JS `new Date`, Python
# `fromisoformat`) obtiene la misma hora --, pero el bot conversacional ya no
# tiene que convertir UTC y deja de decir horas corridas 6 horas.
# Internamente (Mongo, cálculos) todo sigue en UTC.
LocalDateTime = Annotated[datetime, PlainSerializer(to_local, return_type=datetime)]
