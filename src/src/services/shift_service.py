"""
Turnos de las estaciones KDS: inicio, cierre y resumen (tickets + KPIs).

Reglas:

- Iniciar turno abre un registro en `kds_shifts` y activa la estación (empieza a
  recibir solicitudes). Finalizar turno lo cierra y pausa la estación.
- Una estación tiene a lo sumo un turno abierto. `shift_no` cuenta los turnos de
  la estación en el día (hora de Guatemala) y arranca en 1 cada día.
- No se puede finalizar con solicitudes pendientes de aceptar: hay que aceptarlas o
  rechazarlas primero. Las órdenes ya aceptadas se siguen atendiendo después del cierre.
- Un turno que quedó abierto de un día anterior se cierra a la hora de cierre del
  restaurante de ese día (`end_adjusted = true`): al finalizarlo, o automáticamente
  al iniciar el siguiente.

Tickets de un turno: solicitudes creadas dentro de `[started_at, ended_at)` que pasaron
por la estación (ofrecidas, rechazadas o aceptadas por ella). Cada ticket tiene un
`resultado` visto desde la estación (ver RESULT_LABELS). En el resumen diario del
restaurante (sin estación) el resultado es el final del pedido.

KPIs (porcentajes de 0 a 100 sobre `total_tickets`):
- `total_dinero`: suma de `total_amount` de los tickets pagados (aceptados por la estación).
- `rechazadas` = por carga + por falta de stock + sin respuesta + sin motivo.
"""

from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

from src.core.timeutils import (
    as_utc,
    format_local_hhmm,
    local_date_str,
    local_day_bounds_utc,
    parse_iso_date,
    utc_now,
)
from src.models.kds_shift import ShiftStatus
from src.models.order import CancelReason, OrderPaymentStatus, OrderPipelineStatus, describe_order_status
from src.models.order_event import describe_items
from src.models.restaurant import opening_window_for_date
from src.repositories.kds_shift_repo import KdsShiftRepository
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.order_event_repo import OrderEventRepository
from src.repositories.order_repo import OrderRepository
from src.repositories.restaurant_repo import RestaurantRepository


class ShiftError(ValueError):
    """Operación de turno no permitida en el estado actual (409)."""

    def __init__(self, message: str, code: str, **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra

    def detail(self) -> dict:
        return {"message": self.message, "code": self.code, **self.extra}


RESULT_LABELS = {
    "pendiente": "Pendiente de aceptar",
    "esperando_pago": "Esperando pago",
    "en_curso": "En cocina",
    "entregada": "Entregada",
    "no_pagada": "No pagada",
    "cancelada": "Cancelada",
    "rechazada_carga": "Rechazada por carga",
    "rechazada_stock": "Rechazada por falta de stock",
    "sin_respuesta": "Sin respuesta",
    "rechazada_otro": "Rechazada",
}

REJECTED_RESULTS = {"rechazada_carga", "rechazada_stock", "sin_respuesta", "rechazada_otro"}


def _pct(part: int, total: int) -> float:
    return round(part * 100 / total, 1) if total else 0.0


def _rejection_result(reason: str | None) -> str:
    if reason == "kitchen_load":
        return "rechazada_carga"
    if reason == "out_of_stock":
        return "rechazada_stock"
    if reason == CancelReason.NO_RESPONSE.value:
        return "sin_respuesta"
    return "rechazada_otro"


def _accepted_result(order: dict) -> str:
    pipeline = order.get("pipeline_status")
    paid = order.get("payment_status") == OrderPaymentStatus.PAID.value
    if pipeline == OrderPipelineStatus.DELIVERED.value:
        return "entregada"
    if pipeline == OrderPipelineStatus.CANCELLED.value:
        if order.get("payment_status") == OrderPaymentStatus.FAILED.value or order.get("cancel_reason") in (
            CancelReason.PAYMENT_FAILED.value,
            CancelReason.PAYMENT_LINK_EXPIRED.value,
        ):
            return "no_pagada"
        return "cancelada"
    if pipeline == OrderPipelineStatus.ACCEPTED.value and not paid:
        return "esperando_pago"
    return "en_curso"


def classify_for_restaurant(order: dict) -> str:
    """Resultado final de la solicitud para el restaurante."""
    pipeline = order.get("pipeline_status")
    if pipeline == OrderPipelineStatus.PENDING_ACCEPTANCE.value:
        return "pendiente"
    if pipeline == OrderPipelineStatus.REJECTED.value:
        return _rejection_result(order.get("reject_reason") or order.get("cancel_reason"))
    if pipeline == OrderPipelineStatus.CANCELLED.value and not order.get("accepted_at"):
        return "cancelada"
    return _accepted_result(order)


def classify_for_station(order: dict, kds_user_id: str, station_reasons: dict[str, str | None]) -> str:
    """Resultado de la solicitud desde el punto de vista de una estación."""
    if order.get("assigned_kds_id") == kds_user_id:
        return _accepted_result(order)
    if kds_user_id in (order.get("declined_kds_ids") or []):
        reason = station_reasons.get(order["id"])
        if reason is None and order.get("offered_kds_id") == kds_user_id:
            reason = order.get("reject_reason")
        return _rejection_result(reason)
    pipeline = order.get("pipeline_status")
    if pipeline == OrderPipelineStatus.PENDING_ACCEPTANCE.value:
        return "pendiente"
    if pipeline == OrderPipelineStatus.REJECTED.value:
        return _rejection_result(order.get("reject_reason") or order.get("cancel_reason"))
    return "cancelada"


def _handled_by(order: dict, kds_user_id: str | None) -> str | None:
    if kds_user_id:
        return kds_user_id
    declined = order.get("declined_kds_ids") or []
    return order.get("assigned_kds_id") or order.get("offered_kds_id") or (declined[-1] if declined else None)


def compute_kpis(tickets: list[dict]) -> dict:
    total = len(tickets)
    counts: dict[str, int] = {key: 0 for key in RESULT_LABELS}
    for t in tickets:
        counts[t["resultado"]] = counts.get(t["resultado"], 0) + 1
    accepted = [t for t in tickets if t["aceptada"]]
    paid = [t for t in accepted if t["payment_status"] == OrderPaymentStatus.PAID.value]
    money = round(sum(float(t.get("total_amount") or 0) for t in paid), 2)
    rejected = sum(counts[r] for r in REJECTED_RESULTS)
    prep = [t["_prep_minutes"] for t in accepted if t.get("_prep_minutes") is not None]
    return {
        "total_tickets": total,
        "aceptadas": len(accepted),
        "rechazadas": rejected,
        "rechazadas_carga": counts["rechazada_carga"],
        "rechazadas_stock": counts["rechazada_stock"],
        "rechazadas_sin_respuesta": counts["sin_respuesta"],
        "rechazadas_sin_motivo": counts["rechazada_otro"],
        "pendientes": counts["pendiente"],
        "esperando_pago": counts["esperando_pago"],
        "en_curso": counts["en_curso"],
        "entregadas": counts["entregada"],
        "no_pagadas": counts["no_pagada"],
        "canceladas": counts["cancelada"],
        "ordenes_pagadas": len(paid),
        "total_dinero": money,
        "ticket_promedio": round(money / len(paid), 2) if paid else 0.0,
        "pct_aceptadas": _pct(len(accepted), total),
        "pct_rechazadas": _pct(rejected, total),
        "pct_rechazadas_carga": _pct(counts["rechazada_carga"], total),
        "pct_rechazadas_stock": _pct(counts["rechazada_stock"], total),
        "pct_rechazadas_sin_respuesta": _pct(counts["sin_respuesta"], total),
        "tiempo_preparacion_promedio_min": round(sum(prep) / len(prep), 1) if prep else None,
    }


def _minutes(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 60)


def pauses_view(shift: dict, now: datetime) -> dict:
    """Pausas del turno: cantidad, tiempo total y detalle. Una pausa abierta cuenta hasta
    ahora (o hasta el cierre del turno)."""
    limit = as_utc(shift.get("ended_at")) or now
    detail = []
    total = 0.0
    for pause in shift.get("pauses") or []:
        start = as_utc(pause["started_at"])
        end = as_utc(pause.get("ended_at"))
        minutes = _minutes(start, min(end or limit, limit))
        total += minutes
        detail.append(
            {
                "started_at": start,
                "ended_at": end,
                "hora_inicio": format_local_hhmm(start),
                "hora_fin": format_local_hhmm(end) if end else None,
                "duracion_min": round(minutes, 1),
                "started_by": pause.get("started_by"),
                "ended_by": pause.get("ended_by"),
            }
        )
    return {
        "cantidad": len(detail),
        "tiempo_total_min": round(total, 1),
        "en_pausa": bool(detail) and detail[-1]["ended_at"] is None and shift.get("status") == ShiftStatus.OPEN.value,
        "detalle": detail,
    }


def shift_view(shift: dict, now: datetime | None = None) -> dict:
    now = now or utc_now()
    started = as_utc(shift["started_at"])
    ended = as_utc(shift.get("ended_at"))
    pauses = pauses_view(shift, now)
    duration = _minutes(started, ended or now)
    is_open = shift.get("status") == ShiftStatus.OPEN.value
    local_date = shift["local_date"]
    day = parse_iso_date(local_date)
    return {
        "id": shift["id"],
        "restaurant_id": shift.get("restaurant_id"),
        "kds_user_id": shift["kds_user_id"],
        "kds_username": shift.get("kds_username"),
        "local_date": local_date,
        "shift_no": shift["shift_no"],
        "label": f"Turno {shift['shift_no']} · {day.strftime('%d/%m/%Y')}",
        "status": shift.get("status"),
        "started_at": started,
        "ended_at": ended,
        "hora_inicio": format_local_hhmm(started),
        "hora_fin": format_local_hhmm(ended) if ended else None,
        "duracion_min": round(duration),
        "tiempo_activo_min": round(max(0.0, duration - pauses["tiempo_total_min"]), 1),
        "pausas": pauses,
        "stale": is_open and local_date < local_date_str(now),
        "end_adjusted": bool(shift.get("end_adjusted")),
        "started_by": shift.get("started_by"),
        "ended_by": shift.get("ended_by"),
    }


class ShiftService:
    def __init__(self, db):
        self.db = db
        self.shifts = KdsShiftRepository(db)
        self.orders = OrderRepository(db)
        self.events = OrderEventRepository(db)
        self.stations = KdsUserRepository(db)
        self.restaurants = RestaurantRepository(db)

    # --- Estado ---------------------------------------------------------------------------

    async def pending(self, kds_user_id: str) -> dict:
        return {
            "por_aceptar": await self.orders.count_pending_for_station(kds_user_id),
            "en_curso": await self.orders.count_in_progress_for_station(kds_user_id),
        }

    async def current(self, station: dict) -> dict:
        shift = await self.shifts.find_open(station["id"])
        return {
            "shift": shift_view(shift) if shift else None,
            "station_active": bool(station.get("active", True)),
            "pendientes": await self.pending(station["id"]),
            "kpis": (await self.summary(shift, include_tickets=False))["kpis"] if shift else None,
        }

    async def has_open_shift_today(self, kds_user_id: str) -> bool:
        shift = await self.shifts.find_open(kds_user_id)
        return bool(shift) and shift["local_date"] == local_date_str()

    async def record_pause_change(
        self, kds_user_id: str, was_active: bool, active: bool, actor: dict | None = None
    ) -> dict | None:
        """Registra en el turno abierto el inicio (activa -> pausada) o fin (pausada -> activa)
        de una pausa. Sin turno abierto o sin cambio de estado no hace nada."""
        if was_active == active:
            return None
        shift = await self.shifts.find_open(kds_user_id)
        if shift is None:
            return None
        now = utc_now()
        actor = actor or {"type": "system", "id": None, "username": None}
        pauses = [dict(p) for p in shift.get("pauses") or []]
        open_pause = pauses[-1] if pauses and pauses[-1].get("ended_at") is None else None
        if not active and open_pause is None:
            pauses.append({"started_at": now, "ended_at": None, "started_by": actor, "ended_by": None})
        elif active and open_pause is not None:
            open_pause["ended_at"] = now
            open_pause["ended_by"] = actor
        else:
            return None
        return await self.shifts.update_where(
            shift["id"], {"status": ShiftStatus.OPEN.value}, {"pauses": pauses, "updated_at": now}
        )

    # --- Inicio y cierre ------------------------------------------------------------------------

    async def _set_station_active(self, station: dict, active: bool) -> dict:
        from src.integrations.events import event_publisher

        updated = await self.stations.update_one(station["id"], {"active": active})
        await event_publisher.publish_station_status(updated)
        return updated

    async def _publish(self, shift: dict) -> None:
        from src.integrations.events import event_publisher

        await event_publisher.publish_shift_status(shift_view(shift))

    async def _stale_end(self, shift: dict, now: datetime) -> datetime:
        """Fin de un turno que quedó abierto de un día anterior: hora de cierre del restaurante ese día."""
        started = as_utc(shift["started_at"])
        day = parse_iso_date(shift["local_date"])
        end = local_day_bounds_utc(day)[1]
        restaurant = await self.restaurants.find_by_id(shift["restaurant_id"]) if shift.get("restaurant_id") else None
        window = opening_window_for_date(restaurant, day) if restaurant else None
        if window and window[1] > started:
            end = window[1]
        return min(max(end, started), now)

    async def _close(self, shift: dict, actor: dict, now: datetime) -> dict:
        stale = shift["local_date"] < local_date_str(now)
        ended_at = await self._stale_end(shift, now) if stale else now
        pauses = [dict(p) for p in shift.get("pauses") or []]
        for pause in pauses:
            if pause.get("ended_at") is None:
                pause["ended_at"] = max(ended_at, as_utc(pause["started_at"]))
                pause["ended_by"] = actor
        closed = await self.shifts.update_where(
            shift["id"],
            {"status": ShiftStatus.OPEN.value},
            {
                "status": ShiftStatus.CLOSED.value,
                "pauses": pauses,
                "ended_at": ended_at,
                "end_adjusted": stale,
                "ended_by": actor,
                "updated_at": now,
            },
        )
        if closed is None:
            raise ShiftError("El turno ya fue cerrado.", "shift_already_closed")
        return closed

    async def start(self, station: dict, actor: dict) -> dict:
        now = utc_now()
        today = local_date_str(now)
        previous = None
        open_shift = await self.shifts.find_open(station["id"])
        if open_shift is not None:
            if open_shift["local_date"] >= today:
                view = shift_view(open_shift, now)
                raise ShiftError(
                    f"Ya hay un turno abierto ({view['label']}, desde las {view['hora_inicio']}).",
                    "shift_already_open",
                    shift=view,
                )
            previous = await self._close(open_shift, {"type": "system", "id": None, "username": None}, now)

        shift = None
        for _ in range(3):
            document = {
                "restaurant_id": station.get("restaurant_id"),
                "kds_user_id": station["id"],
                "kds_username": station.get("username"),
                "local_date": today,
                "shift_no": await self.shifts.last_shift_no(station["id"], today) + 1,
                "status": ShiftStatus.OPEN.value,
                "started_at": now,
                "ended_at": None,
                "end_adjusted": False,
                "pauses": [],
                "started_by": actor,
                "ended_by": None,
                "created_at": now,
                "updated_at": now,
            }
            try:
                shift = await self.shifts.insert_one(document)
                break
            except DuplicateKeyError:
                if await self.shifts.find_open(station["id"]):
                    raise ShiftError("Ya hay un turno abierto en esta estación.", "shift_already_open")
        if shift is None:
            raise ShiftError("No se pudo iniciar el turno, intenta de nuevo.", "shift_conflict")

        updated_station = await self._set_station_active(station, True)
        if previous is not None:
            await self._publish(previous)
        await self._publish(shift)
        return {
            "shift": shift_view(shift, now),
            "station_active": bool(updated_station.get("active")),
            "previous_shift_closed": shift_view(previous, now) if previous else None,
        }

    async def end(self, station: dict, actor: dict) -> dict:
        now = utc_now()
        shift = await self.shifts.find_open(station["id"])
        if shift is None:
            raise ShiftError("No hay un turno abierto.", "no_open_shift")
        pending = await self.orders.count_pending_for_station(station["id"])
        if pending:
            raise ShiftError(
                f"Tienes {pending} solicitud(es) por aceptar o rechazar antes de finalizar el turno.",
                "pending_requests",
                por_aceptar=pending,
            )
        closed = await self._close(shift, actor, now)
        await self._set_station_active(station, False)
        await self._publish(closed)
        summary = await self.summary(closed)
        summary["pendientes"] = await self.pending(station["id"])
        return summary

    # --- Resúmenes --------------------------------------------------------------------------------

    def _ticket(self, order: dict, resultado: str, kds_user_id: str | None, usernames: dict[str, str]) -> dict:
        prep = None
        started, ready = as_utc(order.get("preparation_started_at")), as_utc(order.get("ready_marked_at"))
        if started and ready:
            prep = (ready - started).total_seconds() / 60
        created = as_utc(order.get("created_at"))
        station_id = _handled_by(order, kds_user_id)
        pipeline = order.get("pipeline_status")
        payment = order.get("payment_status")
        try:
            status_label = describe_order_status(OrderPipelineStatus(pipeline), OrderPaymentStatus(payment))
        except ValueError:
            status_label = pipeline
        accepted = (
            order.get("assigned_kds_id") == kds_user_id if kds_user_id else bool(order.get("accepted_at"))
        )
        return {
            "order_id": order["id"],
            "request_number": order.get("request_number"),
            "order_number": order.get("order_number"),
            "created_at": created,
            "hora": format_local_hhmm(created),
            "items": describe_items(order),
            "total_amount": float(order.get("total_amount") or 0),
            "pipeline_status": pipeline,
            "payment_status": payment,
            "status_label": status_label,
            "resultado": resultado,
            "resultado_label": RESULT_LABELS.get(resultado, resultado),
            "aceptada": accepted and resultado not in REJECTED_RESULTS and resultado != "pendiente",
            "reject_reason": order.get("reject_reason"),
            "out_of_stock_items": order.get("out_of_stock_items") or [],
            "kds_user_id": station_id,
            "kds_username": usernames.get(station_id) if station_id else None,
            "turno": None,
            "_prep_minutes": prep,
        }

    @staticmethod
    def _public(ticket: dict) -> dict:
        return {k: v for k, v in ticket.items() if not k.startswith("_")}

    async def _usernames(self, restaurant_id: str | None) -> dict[str, str]:
        if not restaurant_id:
            return {}
        return {s["id"]: s.get("username") for s in await self.stations.find_for_restaurant(restaurant_id)}

    async def _station_tickets(
        self, restaurant_id: str, kds_user_id: str, orders: list[dict], usernames: dict[str, str]
    ) -> list[dict]:
        reasons: dict[str, str | None] = {}
        for event in await self.events.find_station_rejections([o["id"] for o in orders], kds_user_id):
            reasons[event["order_id"]] = (event.get("data") or {}).get("reject_reason")
        return [
            self._ticket(o, classify_for_station(o, kds_user_id, reasons), kds_user_id, usernames) for o in orders
        ]

    async def summary(self, shift: dict, include_tickets: bool = True) -> dict:
        now = utc_now()
        start = as_utc(shift["started_at"])
        end = as_utc(shift.get("ended_at")) or now + timedelta(seconds=1)
        orders = await self.orders.find_for_station_window(shift["restaurant_id"], shift["kds_user_id"], start, end)
        usernames = {shift["kds_user_id"]: shift.get("kds_username")}
        tickets = await self._station_tickets(shift["restaurant_id"], shift["kds_user_id"], orders, usernames)
        result = {"shift": shift_view(shift, now), "kpis": compute_kpis(tickets)}
        if include_tickets:
            result["tickets"] = [self._public(t) for t in tickets]
        return result

    async def station_shifts_for_day(self, kds_user_id: str, local_date: str) -> list[dict]:
        shifts = await self.shifts.find_for_station_day(kds_user_id, local_date)
        return [await self.summary(s, include_tickets=False) for s in shifts]

    async def get_shift(self, shift_id: str, restaurant_id: str | None = None, kds_user_id: str | None = None):
        try:
            shift = await self.shifts.find_by_id(shift_id)
        except ValueError:
            return None
        if shift is None:
            return None
        if restaurant_id and shift.get("restaurant_id") != restaurant_id:
            return None
        if kds_user_id and shift.get("kds_user_id") != kds_user_id:
            return None
        return shift

    async def day_summary(self, restaurant_id: str, local_date: str, kds_user_id: str | None = None) -> dict:
        """Resumen de un día: KPIs del día, turnos (con sus KPIs) y tickets con el turno al que pertenecen."""
        now = utc_now()
        day = parse_iso_date(local_date)
        day_start, day_end = local_day_bounds_utc(day)
        usernames = await self._usernames(restaurant_id)
        orders = await self.orders.find_for_day(local_date, restaurant_id, kds_user_id)
        if kds_user_id:
            tickets = await self._station_tickets(restaurant_id, kds_user_id, orders, usernames)
        else:
            tickets = [self._ticket(o, classify_for_restaurant(o), None, usernames) for o in orders]

        shifts = await self.shifts.find_overlapping(restaurant_id, day_start, day_end, kds_user_id)
        turnos = [await self.summary(s, include_tickets=False) for s in shifts]
        for ticket in tickets:
            created = ticket["created_at"]
            for item in turnos:
                view = item["shift"]
                ended = view["ended_at"] or now + timedelta(seconds=1)
                if view["kds_user_id"] == ticket["kds_user_id"] and view["started_at"] <= created < ended:
                    ticket["turno"] = {"shift_id": view["id"], "shift_no": view["shift_no"], "label": view["label"]}
                    break

        kpis = compute_kpis(tickets)
        kpis["sin_turno"] = sum(1 for t in tickets if t["turno"] is None)
        return {
            "date": local_date,
            "restaurant_id": restaurant_id,
            "kds_user_id": kds_user_id,
            "kpis": kpis,
            "turnos": turnos,
            "tickets": [self._public(t) for t in tickets],
        }
