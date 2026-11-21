"""
Servicio de pedidos: reglas de negocio sobre el repositorio de orders.

Ciclo de vida (dos campos independientes, ver src/models/order.py):

    pipeline_status:
        pending_acceptance -> accepted -> in_preparation -> ready_for_pickup -> delivered
                           \\-> rejected                  (todas las estaciones rechazaron, o falta de stock)
        (pending_acceptance | accepted, sin pagar) -> cancelled  (manual, pago fallido o link vencido)

    payment_status:
        pending -> paid   (webhook de pago exitoso)
        pending -> failed (pago fallido o link vencido -> cancela el pedido)

Numeración: la solicitud recibe `request_number` al crearse; al confirmarse
el pago se convierte en orden y recibe `order_number`.

Estaciones: la solicitud se ofrece a una sola estación KDS a la vez
(`offered_kds_id`). Si la rechaza, pasa a la siguiente de `station_queue`;
si todas rechazan, queda `rejected`. Aceptar/rechazar usa actualizaciones
condicionales para evitar condiciones de carrera.

Rechazo con motivo (`reject_order`, POST /api/orders/{id}/reject):
`kitchen_load` sigue la misma cadena de estaciones; `out_of_stock` es
definitivo de inmediato (no se reofrece) y desactiva por hoy los productos
indicados (`menu_items.paused_on`).

Ventana de pago: al aceptar se fija `payment_expires_at`
(ver `compute_payment_deadline`); una tarea de fondo cancela las vencidas.

Zona horaria: todo se guarda en UTC; el horario del restaurante y la hora
solicitada por el estudiante se interpretan en hora de Guatemala.

Eventos: cada paso se publica con `event_publisher.publish_order_event`
(registro en `order_events` + WebSocket + Kafka opcional). Publicar nunca
tumba la operación principal.
"""

import logging
import random
import string
from datetime import datetime, timedelta, timezone

from src.configuration import settings
from src.core.timeutils import GUATEMALA_TZ, as_utc, local_date_str, parse_hhmm, to_local, utc_now
from src.integrations.events import event_publisher
from src.models.order import CancelReason, OrderPaymentStatus, OrderPipelineStatus, RejectReason
from src.models.order_event import OrderEventType as E
from src.models.restaurant import MAX_LOOKAHEAD_DAYS, fits_in_opening_hours, next_available_start, service_fee_for
from src.repositories.counter_repo import CounterRepository
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuCategoryRepository, MenuRepository
from src.repositories.order_repo import OrderRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.order import OrderCreate, TimeEstimateRequest
from src.services.menu_service import MenuService, filter_available_today
from src.integrations.tilopay_client import TilopayError
from src.services.payment_service import PaymentService
from src.services.restaurant_availability import (
    CODE_OUT_OF_SERVICE,
    RestaurantUnavailableError,
    ensure_can_order,
    ensure_ready_today,
)
from src.services.station_service import StationError, StationService, busy_minutes_from


logger = logging.getLogger("orders")

# Si Tilopay no responde al revisar un link vencido, se espera este tiempo antes de cancelar igual.
PAYMENT_CHECK_GRACE = timedelta(minutes=10)


class OrderPermissionError(Exception):
    """El actor no puede operar este pedido."""


class OrderConflictError(ValueError):
    """El pedido ya cambió de estado o no está disponible para esta estación."""


class OrderValidationError(Exception):
    """Datos de la operación inválidos para este pedido (422)."""


CODE_BELOW_MINIMUM = "below_minimum_order"


class OrderBelowMinimumError(ValueError):
    """El pedido no llega al monto mínimo (`MIN_ORDER_AMOUNT`). `to_detail()` es el body del 400."""

    def __init__(self, minimum_amount: float, subtotal_amount: float):
        self.minimum_amount = round(minimum_amount, 2)
        self.subtotal_amount = round(subtotal_amount, 2)
        self.missing_amount = round(self.minimum_amount - self.subtotal_amount, 2)
        self.message = (
            f"El pedido tiene que ser de mínimo Q{self.minimum_amount:.2f}. "
            f"Tu pedido suma Q{self.subtotal_amount:.2f}, te faltan Q{self.missing_amount:.2f}: "
            "agrega otro producto para poder hacerlo."
        )
        super().__init__(self.message)

    def to_detail(self) -> dict:
        return {
            "code": CODE_BELOW_MINIMUM,
            "message": self.message,
            "minimum_amount": self.minimum_amount,
            "subtotal_amount": self.subtotal_amount,
            "missing_amount": self.missing_amount,
        }


def _ensure_minimum_amount(subtotal_amount: float) -> None:
    """Pedido mínimo: la suma de productos (sin tarifa de servicio) debe llegar a `MIN_ORDER_AMOUNT`."""
    minimum = settings.payment.min_order_amount
    if minimum and minimum > 0 and round(subtotal_amount, 2) < round(minimum, 2):
        raise OrderBelowMinimumError(minimum, subtotal_amount)


def _generate_otp(length: int = 4) -> str:
    return "".join(random.choices(string.digits, k=length))


def _as_utc(dt: datetime) -> datetime:
    return as_utc(dt)


def _interpret_requested_time(dt: datetime | None) -> datetime | None:
    """Normaliza la hora que manda el estudiante a UTC. Sin zona horaria = hora de Guatemala."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=GUATEMALA_TZ).astimezone(timezone.utc)


def _find_catalog_item(catalog: dict[str, dict], product_name: str) -> dict | None:
    item = catalog.get(product_name)
    if item is not None:
        return item
    wanted = product_name.strip().lower()
    return next((v for k, v in catalog.items() if k.strip().lower() == wanted), None)


def _resolve_modifiers(menu_item: dict, requested: list[str]) -> list[dict]:
    """Nombres de modificadores -> snapshot con precio. Los que no están en el catálogo
    se guardan con precio 0 y `custom = true`."""
    by_name = {m["name"].strip().lower(): m for m in menu_item.get("modifiers") or []}
    resolved = []
    for name in requested or []:
        match = by_name.get(name.strip().lower())
        if match:
            resolved.append({"name": match["name"], "price": float(match.get("price", 0)), "custom": False})
        else:
            resolved.append({"name": name.strip(), "price": 0.0, "custom": True})
    return resolved


def _resolve_order_items(restaurant: dict, items_in: list, catalog: dict[str, dict]) -> tuple[list[dict], float, float]:
    """Valida cada item contra el catálogo de hoy y resuelve precio, modificadores y tiempo de prep."""
    resolved_items = []
    total_amount = 0.0
    order_prep_minutes = 0.0
    requested_units: dict[str, int] = {}
    for item in items_in:
        menu_item = _find_catalog_item(catalog, item.product_name)
        if menu_item is None:
            raise ValueError(f"'{item.product_name}' no pertenece al menú de hoy de '{restaurant['name']}'.")
        if not menu_item.get("available", True):
            raise ValueError(f"'{menu_item['name']}' no está disponible.")
        if menu_item.get("dine_in_only"):
            raise ValueError(f"'{menu_item['name']}' es una promoción, solo válida en restaurante.")
        requested_units[menu_item["id"]] = requested_units.get(menu_item["id"], 0) + item.quantity
        remaining = menu_item.get("remaining_stock")
        if remaining is not None and requested_units[menu_item["id"]] > remaining:
            raise ValueError(f"Solo quedan {remaining} unidades de '{menu_item['name']}' hoy.")

        modifiers = _resolve_modifiers(menu_item, item.modifiers)
        unit_price = float(menu_item["price"])
        modifiers_total = sum(m["price"] for m in modifiers)
        line_total = round((unit_price + modifiers_total) * item.quantity, 2)
        prep_time = float(menu_item.get("prep_time_minutes", 0.0))
        total_amount += line_total
        # Los items se preparan en paralelo: el pedido está listo cuando lo esté el más lento.
        order_prep_minutes = max(order_prep_minutes, prep_time)
        resolved_items.append(
            {
                "product_name": menu_item["name"],
                "quantity": item.quantity,
                "modifiers": modifiers,
                "notes": item.notes,
                "unit_price": unit_price,
                "modifiers_total": round(modifiers_total, 2),
                "line_total": line_total,
                "prep_time_minutes": prep_time,
                "menu_item_id": menu_item["id"],
                "category": menu_item.get("category"),
                "category_icon": menu_item.get("category_icon"),
                "ready_from": menu_item.get("ready_from"),
            }
        )

    return resolved_items, round(total_amount, 2), order_prep_minutes


def _order_amounts(restaurant: dict, subtotal_amount: float) -> dict:
    """Desglose del cobro: productos (`subtotal_amount`) + tarifa de servicio del restaurante.
    `total_amount` es lo que paga el cliente (y lo que se cobra en el link de Tilopay)."""
    service_fee = service_fee_for(restaurant)
    return {
        "subtotal_amount": round(subtotal_amount, 2),
        "service_fee": service_fee,
        "total_amount": round(subtotal_amount + service_fee, 2),
    }


def _overlaps(start: datetime, end: datetime, busy: list[tuple[datetime, datetime]]) -> datetime | None:
    """Fin del primer bloque ocupado que se cruza con `[start, end)`, o None si está libre."""
    for busy_start, busy_end in busy:
        if busy_start < end and start < busy_end:
            return busy_end
    return None


def _slot_is_free(
    start: datetime, prep_minutes: float, busy: list[tuple[datetime, datetime]], restaurant: dict
) -> bool:
    end = start + timedelta(minutes=prep_minutes)
    return _overlaps(start, end, busy) is None and fits_in_opening_hours(restaurant, start, end)


def _first_free_start(
    earliest: datetime, prep_minutes: float, busy: list[tuple[datetime, datetime]], restaurant: dict
) -> tuple[datetime, bool]:
    """Primer inicio `>= earliest` libre en la cocina y dentro de horario.

    Devuelve `(inicio, movido_por_horario)`.
    """
    duration = timedelta(minutes=prep_minutes)
    start = earliest
    moved_by_hours = False
    for _ in range(len(busy) + MAX_LOOKAHEAD_DAYS + 2):
        blocked_until = _overlaps(start, start + duration, busy)
        while blocked_until is not None:
            start = blocked_until
            blocked_until = _overlaps(start, start + duration, busy)
        if fits_in_opening_hours(restaurant, start, start + duration):
            return start, moved_by_hours
        start = next_available_start(restaurant, start, prep_minutes)
        moved_by_hours = True
    raise ValueError(f"'{restaurant.get('name', 'El restaurante')}' no tiene espacio disponible para este pedido.")


def _ready_from_start(now: datetime, items: list[dict]) -> datetime | None:
    """Hora (UTC) desde la que todos los productos del pedido están disponibles hoy."""
    today = to_local(now).date()
    starts = [
        datetime.combine(today, parse_hhmm(i["ready_from"]), tzinfo=GUATEMALA_TZ).astimezone(timezone.utc)
        for i in items
        if i.get("ready_from")
    ]
    return max(starts, default=None)


def _compute_schedule(
    now: datetime,
    busy: list[tuple[datetime, datetime]],
    order_prep_minutes: float,
    requested_ready_time: datetime | None,
    restaurant: dict,
    available_from: datetime | None = None,
) -> tuple[datetime, datetime, bool]:
    """Algoritmo ETC, consciente del horario del restaurante.

    `busy` son los bloques ya comprometidos por la cola pagada: el pedido se
    agenda en el primer hueco libre, así un pedido programado para más tarde
    no retrasa uno que se puede preparar ya. `available_from` es la hora desde
    la que los productos están disponibles (`ready_from`).
    """
    earliest = max(now, available_from) if available_from else now
    prep = timedelta(minutes=order_prep_minutes)

    if requested_ready_time is None:
        start, moved_by_hours = _first_free_start(earliest, order_prep_minutes, busy, restaurant)
        return start, start + prep, not moved_by_hours

    requested_ready_time = _interpret_requested_time(requested_ready_time)
    desired_start = requested_ready_time - prep
    if desired_start >= earliest and _slot_is_free(desired_start, order_prep_minutes, busy, restaurant):
        return desired_start, requested_ready_time, True

    start, _moved = _first_free_start(max(earliest, desired_start), order_prep_minutes, busy, restaurant)
    return start, start + prep, False


def _order_prep_minutes(order: dict) -> float:
    return max((item.get("prep_time_minutes", 0.0) for item in order.get("items", [])), default=0.0)


def compute_payment_deadline(now: datetime, order: dict) -> datetime:
    """Hora límite para pagar un pedido recién aceptado.

    - "Lo antes posible": `PAYMENT_LINK_ASAP_MINUTES`.
    - Con hora solicitada: hora solicitada - tiempo de prep - 5 min, acotado entre
      `PAYMENT_LINK_MIN_MINUTES` y `PAYMENT_LINK_MAX_MINUTES`.
    """
    cfg = settings.payment
    requested = order.get("requested_ready_time")
    if requested is None:
        return now + timedelta(minutes=cfg.asap_minutes)
    latest = as_utc(requested) - timedelta(minutes=_order_prep_minutes(order) + 5)
    window = (latest - now).total_seconds() / 60
    window = max(cfg.min_minutes, min(cfg.max_minutes, window))
    return now + timedelta(minutes=window)


# Transiciones permitidas por PUT /api/orders/{id}/status (pipeline_status).
ALLOWED_TRANSITIONS: dict[OrderPipelineStatus, set[OrderPipelineStatus]] = {
    OrderPipelineStatus.PENDING_ACCEPTANCE: {
        OrderPipelineStatus.ACCEPTED,
        OrderPipelineStatus.REJECTED,
        OrderPipelineStatus.CANCELLED,
    },
    OrderPipelineStatus.ACCEPTED: {OrderPipelineStatus.IN_PREPARATION, OrderPipelineStatus.CANCELLED},
    OrderPipelineStatus.IN_PREPARATION: {OrderPipelineStatus.READY_FOR_PICKUP},
    OrderPipelineStatus.READY_FOR_PICKUP: set(),  # solo avanza vía validate_otp -> delivered
    OrderPipelineStatus.DELIVERED: set(),
    OrderPipelineStatus.REJECTED: set(),
    OrderPipelineStatus.CANCELLED: set(),
}


class OrderService:
    def __init__(
        self,
        repo: OrderRepository,
        menu_repo: MenuRepository,
        restaurant_repo: RestaurantRepository,
        payment_service: PaymentService,
        kds_repo: KdsUserRepository | None = None,
        counter_repo: CounterRepository | None = None,
        category_repo: MenuCategoryRepository | None = None,
    ):
        self.repo = repo
        self.menu_repo = menu_repo
        self.restaurant_repo = restaurant_repo
        self.payment_service = payment_service
        self.kds_repo = kds_repo
        self.counter_repo = counter_repo
        self.stations = StationService(kds_repo, repo)
        self.menu_service = MenuService(menu_repo, repo, category_repo)

    # --- Helpers ------------------------------------------------------------------------

    async def _next_number(self, name: str) -> int | None:
        if self.counter_repo is None:
            return None
        return await self.counter_repo.next(name)

    async def _load_restaurant_and_catalog(self, restaurant_id: str) -> tuple[dict, dict[str, dict]]:
        restaurant = await self.restaurant_repo.find_by_id(restaurant_id)
        if restaurant is None:
            raise ValueError("Restaurante no encontrado.")
        if not restaurant.get("active", True):
            raise RestaurantUnavailableError(
                CODE_OUT_OF_SERVICE, f"{restaurant['name']} no está recibiendo pedidos por el momento."
            )

        menu_items = await self.menu_repo.find_all({"restaurant_id": restaurant_id, "deleted": {"$ne": True}})
        available = await self.menu_service.annotate_stock(filter_available_today(menu_items))
        icon_map = await self.menu_service.category_icon_map(restaurant_id)
        for item in available:
            item["category_icon"] = self.menu_service.icon_for(item.get("category"), icon_map)
        catalog = {item["name"]: item for item in available}
        return restaurant, catalog

    async def _rank_stations(self, restaurant_id: str, resolved_items: list[dict]) -> list[dict]:
        menu_items = [
            {"id": i["menu_item_id"], "name": i["product_name"], "category": i.get("category")} for i in resolved_items
        ]
        try:
            return await self.stations.rank_candidates(restaurant_id, menu_items)
        except StationError as exc:
            raise ValueError(str(exc)) from exc

    async def _station_username(self, kds_user_id: str | None) -> str | None:
        if not kds_user_id or self.kds_repo is None:
            return None
        station = await self.kds_repo.find_by_id(kds_user_id)
        return station.get("username") if station else None

    async def _kitchen_busy(self, restaurant_id: str, kds_user_id: str | None = None) -> list[tuple[datetime, datetime]]:
        return await self.stations.busy_intervals(restaurant_id, kds_user_id)

    @staticmethod
    def _check_restaurant(order: dict, actor: dict | None) -> None:
        if actor is None or actor.get("is_superadmin"):
            return
        if actor.get("restaurant_id") and actor["restaurant_id"] != order.get("restaurant_id"):
            raise OrderPermissionError("Este pedido pertenece a otro restaurante.")

    # --- Consultas y creación ---------------------------------------------------------------

    async def _ensure_open(self, restaurant: dict, now: datetime, requested_ready_time: datetime | None) -> dict:
        """Regla de oro: solo se pide con el restaurante abierto, para HOY y antes del cierre.
        Lanza RestaurantUnavailableError (409 kitchen_closed / kitchen_out_of_service /
        outside_opening_hours, ver src/services/restaurant_availability.py)."""
        stations = await self.stations.restaurant_stations(restaurant["id"])
        return ensure_can_order(restaurant, stations, now, _interpret_requested_time(requested_ready_time))

    async def _schedule_today(
        self, restaurant: dict, availability: dict, now: datetime, station_id: str | None,
        order_prep_minutes: float, requested_ready_time: datetime | None, resolved_items: list[dict],
    ) -> tuple[list, datetime, datetime, bool]:
        busy = await self._kitchen_busy(restaurant["id"], station_id)
        prep_start_time, ready_at, feasible = _compute_schedule(
            now, busy, order_prep_minutes, requested_ready_time, restaurant, _ready_from_start(now, resolved_items)
        )
        ensure_ready_today(restaurant, availability, prep_start_time, ready_at)
        return busy, prep_start_time, ready_at, feasible

    async def estimate(self, data: TimeEstimateRequest) -> dict:
        """POST /api/orders/estimate — hora estimada antes de crear el pedido."""
        restaurant, catalog = await self._load_restaurant_and_catalog(data.restaurant_id)
        now = utc_now()
        availability = await self._ensure_open(restaurant, now, data.desired_ready_time)
        resolved_items, subtotal_amount, order_prep_minutes = _resolve_order_items(restaurant, data.items, catalog)
        _ensure_minimum_amount(subtotal_amount)
        stations = await self._rank_stations(data.restaurant_id, resolved_items)

        station_id = stations[0]["id"] if stations else None
        busy, prep_start_time, ready_at, feasible = await self._schedule_today(
            restaurant, availability, now, station_id, order_prep_minutes, data.desired_ready_time, resolved_items
        )
        notices = [
            f"'{i['product_name']}' está listo aprox. desde las {i['ready_from']}."
            for i in resolved_items
            if i.get("ready_from")
        ]
        return {
            "restaurant_id": data.restaurant_id,
            **_order_amounts(restaurant, subtotal_amount),
            "order_prep_minutes": order_prep_minutes,
            "queue_backlog_minutes": round(busy_minutes_from(busy, now), 1),
            "prep_start_time": prep_start_time,
            "ready_at": ready_at,
            "feasible": feasible,
            "notices": notices,
        }

    async def create_order(self, data: OrderCreate) -> dict:
        """Registra la solicitud (pendiente de aceptación) y la ofrece a la primera estación."""
        restaurant, catalog = await self._load_restaurant_and_catalog(data.restaurant_id)
        now = utc_now()
        availability = await self._ensure_open(restaurant, now, data.requested_ready_time)
        resolved_items, subtotal_amount, order_prep_minutes = _resolve_order_items(restaurant, data.items, catalog)
        _ensure_minimum_amount(subtotal_amount)
        stations = await self._rank_stations(data.restaurant_id, resolved_items)

        offered = stations[0] if stations else None
        # Que el pedido alcance a estar listo hoy antes del cierre (no se pasa al día siguiente).
        await self._schedule_today(
            restaurant, availability, now, offered["id"] if offered else None,
            order_prep_minutes, data.requested_ready_time, resolved_items,
        )
        document = {
            "student_id": data.student_id,
            "restaurant_id": data.restaurant_id,
            "items": resolved_items,
            "pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value,
            "payment_status": OrderPaymentStatus.PENDING.value,
            # Snapshot: la tarifa vigente al crear el pedido (cambiarla después no lo afecta).
            **_order_amounts(restaurant, subtotal_amount),
            "requested_ready_time": _interpret_requested_time(data.requested_ready_time),
            "prep_start_time": None,
            "ready_at": None,
            "otp_code": None,
            "payment_reference": None,
            "request_number": await self._next_number("request_number"),
            "order_number": None,
            "station_queue": [s["id"] for s in stations],
            "offered_kds_id": offered["id"] if offered else None,
            "declined_kds_ids": [],
            "assigned_kds_id": None,
            "payment_expires_at": None,
            "offered_at": now if offered else None,
            "accepted_at": None,
            "paid_at": None,
            "preparation_started_at": None,
            "ready_marked_at": None,
            "delivered_at": None,
            "cancelled_at": None,
            "cancel_reason": None,
            "local_date": local_date_str(now),
            "created_at": now,
            "updated_at": now,
        }
        order = await self.repo.insert_one(document)
        await event_publisher.publish_order_event(
            E.ORDER_CREATED.value, order, extra={"station_username": offered["username"] if offered else None}
        )
        return order

    async def get_active_orders(self, actor: dict | None = None) -> list[dict]:
        """Cola visible del KDS: solo el restaurante y la estación del token."""
        if actor is None:
            return await self.repo.find_active()
        if actor["type"] == "kds":
            return await self.repo.find_active(actor.get("restaurant_id"), actor["id"])
        return await self.repo.find_active(None if actor.get("is_superadmin") else actor.get("restaurant_id"))

    async def get_order_status(self, order_id: str) -> dict | None:
        return await self.repo.find_by_id(order_id)

    async def _preview_ready_at(self, order: dict) -> datetime:
        restaurant = await self.restaurant_repo.find_by_id(order["restaurant_id"])
        now = utc_now()
        busy = await self._kitchen_busy(order["restaurant_id"], order.get("assigned_kds_id"))
        _start, ready_at, _feasible = _compute_schedule(
            now,
            busy,
            _order_prep_minutes(order),
            order.get("requested_ready_time"),
            restaurant,
            _ready_from_start(now, order.get("items", [])),
        )
        return ready_at

    # --- Cambios de estado desde KDS / administración -------------------------------------------

    async def update_status(
        self, order_id: str, pipeline_status: OrderPipelineStatus, actor: dict | None = None
    ) -> dict | None:
        """PUT /api/orders/{id}/status. Solo KDS/administración, nunca el bot."""
        order = await self.repo.find_by_id(order_id)
        if order is None:
            return None
        self._check_restaurant(order, actor)

        current = OrderPipelineStatus(order["pipeline_status"])
        if pipeline_status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"No se puede pasar de '{current.value}' a '{pipeline_status.value}'.")

        payment_status = OrderPaymentStatus(order["payment_status"])
        is_kds = bool(actor and actor.get("type") == "kds")
        actor_id = actor.get("id") if is_kds else None

        if current == OrderPipelineStatus.PENDING_ACCEPTANCE and pipeline_status in (
            OrderPipelineStatus.ACCEPTED,
            OrderPipelineStatus.REJECTED,
        ):
            if actor is not None and not is_kds and order.get("offered_kds_id"):
                raise OrderPermissionError("Solo la estación KDS que recibió la solicitud puede aceptarla o rechazarla.")
            if is_kds and order.get("offered_kds_id") and order["offered_kds_id"] != actor_id:
                raise OrderPermissionError("Esta solicitud está asignada a otra estación.")
        elif current == OrderPipelineStatus.PENDING_ACCEPTANCE:
            if is_kds and order.get("offered_kds_id") and order["offered_kds_id"] != actor_id:
                raise OrderPermissionError("Esta solicitud está asignada a otra estación.")
        elif is_kds and order.get("assigned_kds_id") and order["assigned_kds_id"] != actor_id:
            raise OrderPermissionError("Esta orden la atiende otra estación.")

        if pipeline_status == OrderPipelineStatus.CANCELLED and payment_status == OrderPaymentStatus.PAID:
            raise ValueError("No se puede cancelar un pedido que ya fue pagado.")
        if pipeline_status == OrderPipelineStatus.IN_PREPARATION and payment_status != OrderPaymentStatus.PAID:
            raise ValueError("No se puede empezar a preparar un pedido que todavía no está pagado.")

        if pipeline_status == OrderPipelineStatus.ACCEPTED:
            return await self._accept(order, actor)
        if pipeline_status == OrderPipelineStatus.REJECTED:
            return await self._reject(order, actor)

        now = utc_now()
        guard = {"pipeline_status": current.value}
        if pipeline_status == OrderPipelineStatus.IN_PREPARATION:
            fields, event = {"preparation_started_at": now}, E.ORDER_IN_PREPARATION
        elif pipeline_status == OrderPipelineStatus.READY_FOR_PICKUP:
            fields, event = {"ready_marked_at": now}, E.ORDER_READY
        else:  # CANCELLED
            guard["payment_status"] = {"$ne": OrderPaymentStatus.PAID.value}
            fields, event = {"cancelled_at": now, "cancel_reason": CancelReason.CANCELLED.value}, E.ORDER_CANCELLED

        updated = await self.repo.update_where(
            order_id, guard, {"pipeline_status": pipeline_status.value, "updated_at": now, **fields}
        )
        if updated is None:
            raise OrderConflictError("El pedido cambió de estado, actualiza la pantalla.")
        if pipeline_status == OrderPipelineStatus.CANCELLED:
            await self.payment_service.discard_link(updated)
        await event_publisher.publish_order_event(event.value, updated, actor=actor)
        return updated

    async def _accept(self, order: dict, actor: dict | None) -> dict:
        now = utc_now()
        order_id = order["id"]
        # Puede lanzar PaymentGatewayError: la solicitud queda pendiente y el KDS reintenta.
        link = await self.payment_service.request_payment_link(order)
        payment_expires_at = compute_payment_deadline(now, order)
        guard = {
            "pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value,
            "offered_kds_id": order.get("offered_kds_id"),
        }
        assigned = order.get("offered_kds_id") or (actor.get("id") if actor and actor.get("type") == "kds" else None)
        updated = await self.repo.update_where(
            order_id,
            guard,
            {
                "pipeline_status": OrderPipelineStatus.ACCEPTED.value,
                "payment_status": OrderPaymentStatus.PENDING.value,
                "payment_reference": link.url,
                "payment_provider": link.provider,
                "payment_link_id": link.link_id,
                "payment_account_user": link.account_user,
                "assigned_kds_id": assigned,
                "accepted_at": now,
                "payment_expires_at": payment_expires_at,
                "updated_at": now,
            },
        )
        if updated is None:
            await self.payment_service.discard_link(
                {
                    "id": order_id,
                    "restaurant_id": order["restaurant_id"],
                    "payment_provider": link.provider,
                    "payment_link_id": link.link_id,
                    "payment_account_user": link.account_user,
                }
            )
            raise OrderConflictError("La solicitud ya no está disponible para esta estación.")
        preview_ready_at = await self._preview_ready_at(updated)
        await event_publisher.publish_order_event(
            E.ORDER_ACCEPTED.value,
            updated,
            extra={
                "preview_ready_at": preview_ready_at.isoformat(),
                "payment_expires_at": payment_expires_at.isoformat(),
            },
            actor=actor,
        )
        await event_publisher.publish_order_event(E.PAYMENT_LINK_SENT.value, updated)
        return updated

    async def reject_order(
        self,
        order_id: str,
        reason: RejectReason,
        out_of_stock_item_ids: list[str] | None = None,
        actor: dict | None = None,
    ) -> dict | None:
        """POST /api/orders/{id}/reject — la cocina rechaza una solicitud indicando el motivo."""
        order = await self.repo.find_by_id(order_id)
        if order is None:
            return None
        self._check_restaurant(order, actor)
        if order["pipeline_status"] != OrderPipelineStatus.PENDING_ACCEPTANCE.value:
            raise OrderConflictError("Solo se puede rechazar una solicitud pendiente de aceptación.")

        is_kds = bool(actor and actor.get("type") == "kds")
        if actor is not None and not is_kds and order.get("offered_kds_id"):
            raise OrderPermissionError("Solo la estación KDS que recibió la solicitud puede aceptarla o rechazarla.")
        if is_kds and order.get("offered_kds_id") and order["offered_kds_id"] != actor.get("id"):
            raise OrderPermissionError("Esta solicitud está asignada a otra estación.")

        out_of_stock_items: list[dict] = []
        if reason == RejectReason.OUT_OF_STOCK:
            names = {i.get("menu_item_id"): i.get("product_name") for i in order.get("items", [])}
            unknown = [i for i in out_of_stock_item_ids or [] if i not in names]
            if unknown:
                raise OrderValidationError(f"Estos items no pertenecen al pedido: {', '.join(unknown)}.")
            out_of_stock_items = [{"menu_item_id": i, "name": names[i]} for i in out_of_stock_item_ids or []]
            if not out_of_stock_items:
                raise OrderValidationError("Indica qué productos del pedido no tienen stock.")
        return await self._reject(order, actor, reason, out_of_stock_items)

    async def _reject(
        self,
        order: dict,
        actor: dict | None,
        reason: RejectReason | None = None,
        out_of_stock_items: list[dict] | None = None,
    ) -> dict:
        now = utc_now()
        order_id = order["id"]
        rejecting = order.get("offered_kds_id")
        declined = [*(order.get("declined_kds_ids") or []), *([rejecting] if rejecting else [])]
        reason_value = reason.value if reason else None
        out_of_stock = reason == RejectReason.OUT_OF_STOCK
        next_station = None
        # Sin stock no tiene sentido reofrecer: el producto se desactiva para todo el restaurante.
        if self.kds_repo is not None and not out_of_stock:
            for kds_id in order.get("station_queue") or []:
                if kds_id in declined:
                    continue
                station = await self.kds_repo.find_by_id(kds_id)
                if station and station.get("active", True):
                    next_station = station
                    break

        guard = {"pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value, "offered_kds_id": rejecting}
        if next_station is not None:
            updated = await self.repo.update_where(
                order_id,
                guard,
                {
                    "offered_kds_id": next_station["id"],
                    "declined_kds_ids": declined,
                    "offered_at": now,
                    "updated_at": now,
                },
            )
            if updated is None:
                raise OrderConflictError("La solicitud ya no está disponible para esta estación.")
            await event_publisher.publish_order_event(
                E.ORDER_REJECTED_BY_STATION.value,
                updated,
                extra={"next_station_username": next_station["username"], "reject_reason": reason_value},
                actor=actor,
                target_kds_ids=[rejecting] if rejecting else [],
            )
            await event_publisher.publish_order_event(
                E.ORDER_OFFERED.value,
                updated,
                extra={"station_username": next_station["username"]},
                target_kds_ids=[next_station["id"]],
            )
            return updated

        updated = await self.repo.update_where(
            order_id,
            guard,
            {
                "pipeline_status": OrderPipelineStatus.REJECTED.value,
                "declined_kds_ids": declined,
                "cancelled_at": now,
                "cancel_reason": (
                    CancelReason.OUT_OF_STOCK if out_of_stock else CancelReason.REJECTED_BY_ALL_STATIONS
                ).value,
                "reject_reason": reason_value,
                "out_of_stock_items": out_of_stock_items or [],
                "updated_at": now,
            },
        )
        if updated is None:
            raise OrderConflictError("La solicitud ya no está disponible para esta estación.")
        if out_of_stock:
            # Desactivación solo por hoy: el chatbot deja de ofrecerlos y mañana vuelven solos.
            await self.menu_service.pause_items_today(
                order["restaurant_id"], [i["menu_item_id"] for i in out_of_stock_items or []]
            )
        await event_publisher.publish_order_event(
            E.ORDER_REJECTED.value,
            updated,
            extra={"reject_reason": reason_value},
            actor=actor,
            target_kds_ids=[rejecting] if rejecting else [],
        )
        return updated

    # --- Pago -------------------------------------------------------------------------------------

    async def mark_paid_and_queue(self, order_id: str) -> dict | None:
        """Pago confirmado: la solicitud se convierte en orden y entra a la cola de cocina.

        El horario se recalcula con la cola actual de la estación; si el estudiante pidió
        una hora y todavía se alcanza, se respeta.
        """
        order = await self.repo.find_by_id(order_id)
        if (
            order is None
            or order["pipeline_status"] != OrderPipelineStatus.ACCEPTED.value
            or order["payment_status"] != OrderPaymentStatus.PENDING.value
        ):
            return None

        restaurant = await self.restaurant_repo.find_by_id(order["restaurant_id"])
        now = utc_now()
        busy = await self._kitchen_busy(order["restaurant_id"], order.get("assigned_kds_id"))
        prep_start_time, ready_at, _feasible = _compute_schedule(
            now,
            busy,
            _order_prep_minutes(order),
            order.get("requested_ready_time"),
            restaurant,
            _ready_from_start(now, order.get("items", [])),
        )

        updated = await self.repo.update_where(
            order_id,
            {"pipeline_status": OrderPipelineStatus.ACCEPTED.value, "payment_status": OrderPaymentStatus.PENDING.value},
            {
                "payment_status": OrderPaymentStatus.PAID.value,
                "otp_code": _generate_otp(),
                "prep_start_time": prep_start_time,
                "ready_at": ready_at,
                "order_number": await self._next_number("order_number"),
                "paid_at": now,
                "updated_at": now,
            },
        )
        if updated is not None:
            await event_publisher.publish_order_event(E.ORDER_PAID.value, updated)
        return updated

    async def cancel_for_nonpayment(self, order_id: str, expired: bool = False) -> dict | None:
        """Pago fallido (webhook) o link vencido: la solicitud se cancela."""
        now = utc_now()
        reason = CancelReason.PAYMENT_LINK_EXPIRED if expired else CancelReason.PAYMENT_FAILED
        updated = await self.repo.update_where(
            order_id,
            {"pipeline_status": OrderPipelineStatus.ACCEPTED.value, "payment_status": OrderPaymentStatus.PENDING.value},
            {
                "pipeline_status": OrderPipelineStatus.CANCELLED.value,
                "payment_status": OrderPaymentStatus.FAILED.value,
                "cancelled_at": now,
                "cancel_reason": reason.value,
                "updated_at": now,
            },
        )
        if updated is not None:
            await self.payment_service.discard_link(updated)
            event = E.PAYMENT_LINK_EXPIRED if expired else E.ORDER_PAYMENT_FAILED
            await event_publisher.publish_order_event(event.value, updated)
        return updated

    async def confirm_gateway_payment(self, order_id: str) -> str:
        """Aviso de la pasarela (webhook o retorno del cliente): se consulta a
        Tilopay y, si el link tiene un pago aprobado, la solicitud pasa a orden.

        Devuelve: `paid`, `already_paid`, `not_paid`, `late` (pagó un pedido ya
        cancelado: requiere reembolso manual), `not_found` o `error`.
        """
        order = await self.repo.find_by_id(order_id)
        if order is None:
            return "not_found"
        if order["payment_status"] == OrderPaymentStatus.PAID.value:
            return "already_paid"
        try:
            if not await self.payment_service.is_paid(order):
                return "not_paid"
        except TilopayError as exc:
            # La tarea de vencimiento vuelve a consultar antes de cancelar.
            logger.error("No se pudo verificar el pago de %s en Tilopay: %s", order_id, exc)
            return "error"
        if await self.mark_paid_and_queue(order_id) is not None:
            return "paid"
        current = await self.repo.find_by_id(order_id)
        if current and current["payment_status"] == OrderPaymentStatus.PAID.value:
            return "already_paid"
        logger.error("PAGO RECIBIDO PARA UN PEDIDO CANCELADO (%s): requiere reembolso manual en Tilopay.", order_id)
        await self.repo.update_fields(order_id, {"late_payment_at": utc_now()})
        return "late"

    async def expire_unpaid_orders(self, now: datetime | None = None) -> int:
        """Cancela las solicitudes aceptadas cuyo link de pago venció."""
        now = now or utc_now()
        expired = await self.repo.find_expired_unpaid(now)
        count = 0
        for order in expired:
            # El webhook de Tilopay no se reintenta: antes de cancelar se
            # confirma con Tilopay que de verdad no pagó.
            try:
                if await self.payment_service.is_paid(order):
                    await self.mark_paid_and_queue(order["id"])
                    continue
            except TilopayError as exc:
                if now - as_utc(order["payment_expires_at"]) < PAYMENT_CHECK_GRACE:
                    logger.warning("Tilopay no respondió al revisar %s; se reintenta: %s", order["id"], exc)
                    continue
            if await self.cancel_for_nonpayment(order["id"], expired=True):
                count += 1
        return count

    async def expire_unanswered_requests(self, now: datetime | None = None) -> int:
        """Rechaza las solicitudes que el restaurante no atendió en `request_timeout_minutes`.

        Solo toca `pending_acceptance`: lo aceptado o pagado nunca se cierra solo (para entregarlo se
        exige el código en mostrador). El estudiante recibe el aviso de "el restaurante no respondió".
        """
        timeout = settings.payment.request_timeout_minutes
        if timeout <= 0:
            return 0
        now = now or utc_now()
        stale = await self.repo.find_unanswered_requests(now - timedelta(minutes=timeout))
        count = 0
        for order in stale:
            updated = await self.repo.update_where(
                order["id"],
                {"pipeline_status": OrderPipelineStatus.PENDING_ACCEPTANCE.value},
                {
                    "pipeline_status": OrderPipelineStatus.REJECTED.value,
                    "cancelled_at": now,
                    "cancel_reason": CancelReason.NO_RESPONSE.value,
                    "reject_reason": CancelReason.NO_RESPONSE.value,
                    "out_of_stock_items": [],
                    "updated_at": now,
                },
            )
            if updated is None:  # la atendieron justo en este momento
                continue
            await event_publisher.publish_order_event(
                E.ORDER_REJECTED.value, updated, extra={"reject_reason": CancelReason.NO_RESPONSE.value}
            )
            count += 1
        return count

    # --- Entrega -------------------------------------------------------------------------------------

    async def validate_otp(self, order_id: str, otp_code: str, actor: dict | None = None) -> bool:
        """POST /api/orders/{id}/validate-otp — entrega en mostrador."""
        order = await self.repo.find_by_id(order_id)
        if order is None:
            return False
        self._check_restaurant(order, actor)
        if order.get("pipeline_status") != OrderPipelineStatus.READY_FOR_PICKUP.value:
            return False
        if order.get("otp_code") != otp_code:
            await event_publisher.publish_order_event(E.OTP_FAILED.value, order, actor=actor)
            return False
        now = utc_now()
        updated = await self.repo.update_where(
            order_id,
            {"pipeline_status": OrderPipelineStatus.READY_FOR_PICKUP.value},
            {"pipeline_status": OrderPipelineStatus.DELIVERED.value, "delivered_at": now, "updated_at": now},
        )
        if updated is None:
            return False
        await event_publisher.publish_order_event(E.ORDER_DELIVERED.value, updated, actor=actor)
        return True
