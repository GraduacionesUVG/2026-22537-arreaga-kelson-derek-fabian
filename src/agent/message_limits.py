"""
Límite de mensajes salientes por usuario.

WhatsApp cobra por cada mensaje que manda el bot, así que se lleva la cuenta
de cuántos se le han mandado a cada número en el día y, al llegar al tope, el
webhook deja de contestar (y de gastar turnos de OpenAI) hasta el día siguiente.
"""

import os
from datetime import date, datetime, timedelta, timezone

# Tope de mensajes salientes por número y por día
MAX_DAILY_MESSAGES_PER_USER = int(os.getenv("MAX_DAILY_MESSAGES_PER_USER", "30"))
_LOCAL_TZ = timezone(timedelta(hours=-6))

LIMIT_REACHED_MESSAGE = (
    "Llegaste al límite de mensajes de hoy 🙏 Mañana con gusto seguimos. "
    "Si tu pedido ya está en marcha, no te preocupes: sigue su curso."
)

class _Usage:
    def __init__(self, day: date):
        self.day = day
        self.sent = 0
        self.notified = False  

_usage: dict[str, _Usage] = {}


def _today() -> date:
    return datetime.now(_LOCAL_TZ).date()


def _get(wa_id: str) -> _Usage:
    usage = _usage.get(wa_id)
    if usage is None or usage.day != _today():
        usage = _usage[wa_id] = _Usage(_today())
    return usage


def limit_reached(wa_id: str) -> bool:
    if MAX_DAILY_MESSAGES_PER_USER <= 0:
        return False
    return _get(wa_id).sent >= MAX_DAILY_MESSAGES_PER_USER


def record_sent(wa_id: str, count: int = 1) -> None:
    _get(wa_id).sent += count


def should_notify_limit(wa_id: str) -> bool:
    usage = _get(wa_id)
    if usage.notified:
        return False
    usage.notified = True
    return True
