"""
Forma del documento de Mongo para `kds_shifts` — turnos de trabajo de una estación KDS.

Un turno marca el intervalo `[started_at, ended_at)` en el que la estación estuvo
recibiendo pedidos. Los tickets y KPIs del turno NO se copian aquí: se calculan
sobre `orders` (y `order_events` para el motivo de rechazo de cada estación)
filtrando por estación y por ese intervalo, así que siempre reflejan el estado
actual de cada pedido (por ejemplo, un pedido aceptado en el turno y pagado
después del cierre suma al dinero del turno).

- `local_date`: fecha (hora de Guatemala) en la que se inició el turno.
- `shift_no`: número del turno de la estación en `local_date` (1, 2, 3...). Se
  calcula al abrir como el último `shift_no` de esa estación y fecha + 1, por lo
  que el conteo arranca en 1 cada día sin un reseteo explícito. Índice único
  `(kds_user_id, local_date, shift_no)`.
- Una estación tiene a lo sumo un turno `open` (índice único parcial).
- `pauses`: pausas de la estación durante el turno (`PUT /api/kds/me/active` o desde el
  panel). Cada una `{started_at, ended_at, started_by, ended_by}`; `ended_at = null`
  mientras sigue en pausa. Al cerrar el turno, una pausa abierta se cierra con el turno.
- `end_adjusted = true`: el turno se cerró un día después de iniciado (quedó
  abierto); `ended_at` se fija a la hora de cierre del restaurante de ese día.

Ejemplo:
{
    "_id": ObjectId("..."),
    "restaurant_id": "665f1c...",
    "kds_user_id": "66a0...",
    "kds_username": "gh-uvg",
    "local_date": "2026-10-14",
    "shift_no": 2,
    "status": "closed",
    "started_at": ISODate("2026-10-14T19:00:00Z"),
    "ended_at": ISODate("2026-10-15T01:30:00Z"),
    "end_adjusted": false,
    "pauses": [
        {"started_at": ISODate("2026-10-14T21:10:00Z"), "ended_at": ISODate("2026-10-14T21:25:00Z"),
         "started_by": {"type": "kds", "id": "66a0...", "username": "gh-uvg"},
         "ended_by": {"type": "kds", "id": "66a0...", "username": "gh-uvg"}}
    ],
    "started_by": {"type": "kds", "id": "66a0...", "username": "gh-uvg"},
    "ended_by": {"type": "kds", "id": "66a0...", "username": "gh-uvg"},
    "created_at": ISODate("..."),
    "updated_at": ISODate("...")
}
"""

from enum import Enum

COLLECTION_NAME = "kds_shifts"


class ShiftStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
