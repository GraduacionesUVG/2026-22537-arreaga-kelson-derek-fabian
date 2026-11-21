"""
Forma del documento de Mongo para la colección `orders`.

A diferencia de src/schemas/order.py (que valida lo que entra/sale por la
API HTTP), este módulo describe la forma que tiene el documento tal como
vive en MongoDB. Como Mongo es schemaless, esto es documentación + type
hints para desarrollo, no una validación forzada por el motor de base de
datos (esa validación ya la hace la capa de schemas antes de llegar aquí).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class OrderPipelineStatus(str, Enum):
    """Progreso físico del pedido (cocina/mostrador) -- independiente de si ya se pagó.

    `ACCEPTED` cubre DOS momentos distintos que antes eran estados separados
    (`awaiting_payment` y `accepted`): "el restaurante dijo que sí, falta pagar"
    y "ya pagó, está en cola para que la cocina empiece" -- se distinguen por
    `OrderPaymentStatus`, no por el pipeline. Ver `describe_order_status()`.
    """

    PENDING_ACCEPTANCE = "pending_acceptance"  # Recién creado, el restaurante no ha respondido
    ACCEPTED = "accepted"  # Restaurante aceptó (pendiente de pago, o ya pagado y en cola)
    IN_PREPARATION = "in_preparation"  # En preparación (requiere payment_status=paid)
    READY_FOR_PICKUP = "ready_for_pickup"  # Listo para recoger
    DELIVERED = "delivered"  # Entregado (confirmado con OTP) -- estado terminal
    REJECTED = "rejected"  # Restaurante rechazó el pedido -- estado terminal
    CANCELLED = "cancelled"  # Cancelado (por el usuario, o por falta de pago) -- estado terminal


class OrderPaymentStatus(str, Enum):
    PENDING = "pending"  # Aún no se ha cobrado (o el cobro está en curso)
    PAID = "paid"  # Pago confirmado con la pasarela (Tilopay)
    FAILED = "failed"  # El pago no se completó (webhook "failed", o venció el link) -- pedido pasa a cancelled


class CancelReason(str, Enum):
    """Motivo de cierre sin entrega (`orders.cancel_reason`)."""

    REJECTED_BY_ALL_STATIONS = "rejected_by_all_stations"
    OUT_OF_STOCK = "out_of_stock"
    PAYMENT_FAILED = "payment_failed"
    PAYMENT_LINK_EXPIRED = "payment_link_expired"
    NO_RESPONSE = "no_response"  # el restaurante no aceptó ni rechazó a tiempo (REQUEST_ACCEPTANCE_TIMEOUT_MINUTES)
    CANCELLED = "cancelled"


class RejectReason(str, Enum):
    """Por qué la cocina rechazó una solicitud (`orders.reject_reason`)."""

    KITCHEN_LOAD = "kitchen_load"  # mucha carga: se le pide al cliente intentar más tarde
    OUT_OF_STOCK = "out_of_stock"  # faltan productos: se desactivan por hoy (`orders.out_of_stock_items`)


# Estados terminales: el pedido ya no se mueve más y no debe contar como "activo"
# en la cola visible del KDS ni en el backlog del algoritmo de ETC.
TERMINAL_PIPELINE_STATUSES = frozenset(
    {OrderPipelineStatus.DELIVERED, OrderPipelineStatus.REJECTED, OrderPipelineStatus.CANCELLED}
)


def is_active_pipeline_status(pipeline_status: OrderPipelineStatus) -> bool:
    return pipeline_status not in TERMINAL_PIPELINE_STATUSES


def describe_order_status(pipeline_status: OrderPipelineStatus, payment_status: OrderPaymentStatus) -> str:
    """Etiqueta humana (español) para mostrarle al cliente vía el bot -- única
    fuente de verdad para no repetir esta lógica en cada schema/endpoint."""
    if pipeline_status == OrderPipelineStatus.PENDING_ACCEPTANCE:
        return "Pendiente de aprobación"
    if pipeline_status == OrderPipelineStatus.ACCEPTED:
        if payment_status == OrderPaymentStatus.PAID:
            return "Pagado, pendiente de preparación"
        return "Aceptado por el restaurante, pendiente de pago"
    if pipeline_status == OrderPipelineStatus.IN_PREPARATION:
        return "En preparación"
    if pipeline_status == OrderPipelineStatus.READY_FOR_PICKUP:
        return "Listo para recoger"
    if pipeline_status == OrderPipelineStatus.DELIVERED:
        return "Entregado"
    if pipeline_status == OrderPipelineStatus.REJECTED:
        return "Rechazado por el restaurante"
    if pipeline_status == OrderPipelineStatus.CANCELLED:
        if payment_status == OrderPaymentStatus.FAILED:
            return "Cancelado por falta de pago"
        return "Cancelado"
    return pipeline_status.value


SERVICE_FEE_LABEL = "Tarifa de servicio"


def describe_price_breakdown(subtotal_amount: float | None, service_fee: float, total_amount: float) -> str:
    """Desglose del cobro en texto, listo para que el bot lo repita tal cual:
    "Subtotal Q60.00 + Tarifa de servicio Q2.65 = Total Q62.65" (o "Total Q60.00" sin tarifa).
    Va como texto (no solo números) porque los guardrails del agente validan los nombres de
    las líneas con precio contra el texto que devolvieron las funciones."""
    fee = float(service_fee or 0)
    total = float(total_amount or 0)
    if fee <= 0:
        return f"Total Q{total:.2f}"
    subtotal = float(subtotal_amount) if subtotal_amount is not None else total - fee
    return f"Subtotal Q{subtotal:.2f} + {SERVICE_FEE_LABEL} Q{fee:.2f} = Total Q{total:.2f}"


COLLECTION_NAME = "orders"


@dataclass
class OrderItemDocument:
    product_name: str
    quantity: int
    modifiers: list[str] = field(default_factory=list)
    notes: str | None = None
    unit_price: float = 0.0
    prep_time_minutes: float = 0.0
    menu_item_id: str | None = None
    category: str | None = None
    category_icon: str | None = None  # clave del icono de la categoría al crear el pedido
    line_total: float = 0.0  # copiado de menu_items.prep_time_minutes al crear el pedido (snapshot)


@dataclass
class OrderDocument:
    """Forma esperada de un documento en la colección `orders`.

    Ejemplo real (JSON) tal como queda almacenado en Mongo:

    {
        "_id": ObjectId("665f1b2c8f1b2c8f1b2c8f1b"),
        "student_id": "5024-carne-demo",
        "restaurant_id": "rest-001",
        "items": [
            {"product_name": "Menú 1", "quantity": 1, "modifiers": [], "unit_price": 35.0, "prep_time_minutes": 8}
        ],
        "pipeline_status": "pending_acceptance",
        "payment_status": "pending",
        "total_amount": 35.0,
        "requested_ready_time": null,
        "prep_start_time": null,
        "ready_at": null,
        "otp_code": null,
        "payment_reference": null,
        "created_at": ISODate("2026-07-23T10:00:00Z"),
        "updated_at": ISODate("2026-07-23T10:00:00Z")
    }

    `total_amount`, `unit_price` y `prep_time_minutes` de cada item se
    calculan una sola vez en OrderService.create_order a partir de
    `menu_items` (patrón snapshot).
    `payment_reference` lo llena PaymentService.request_payment_link cuando
    el restaurante acepta el pedido (`pipeline_status` pasa a `accepted` con
    `payment_status` todavía `pending`; simulado mientras no hay integración
    real con BAC).

    `requested_ready_time` es lo que pide el estudiante al crear el pedido
    (opcional -- si no manda nada, se entiende "lo antes posible").
    `prep_start_time`/`ready_at` los calcula OrderService.mark_paid_and_queue
    (algoritmo ETC) cuando se confirma el pago
    (`payment_status` -> `paid`) y el pedido entra de verdad a la cola de cocina.
    """

    student_id: str
    restaurant_id: str
    items: list[OrderItemDocument]
    pipeline_status: OrderPipelineStatus = OrderPipelineStatus.PENDING_ACCEPTANCE
    payment_status: OrderPaymentStatus = OrderPaymentStatus.PENDING
    total_amount: float = 0.0
    requested_ready_time: datetime | None = None
    prep_start_time: datetime | None = None
    ready_at: datetime | None = None
    otp_code: str | None = None
    payment_reference: str | None = None  # URL del link de pago
    payment_provider: str | None = None  # "tilopay" | "mock"
    payment_account_user: str | None = None  # api_user de la cuenta de Tilopay del restaurante donde se creó el link
    payment_link_id: str | None = None  # id del link en Tilopay
    late_payment_at: datetime | None = None  # pagó un pedido ya cancelado (reembolso manual)
    # Numeración visible: Solicitud #request_number, Orden #order_number (se asigna al pagar).
    request_number: int | None = None
    order_number: int | None = None
    # Estaciones KDS (ver src/services/station_service.py).
    station_queue: list[str] = field(default_factory=list)
    offered_kds_id: str | None = None
    declined_kds_ids: list[str] = field(default_factory=list)
    assigned_kds_id: str | None = None
    # Ventana de pago y marcas de tiempo reales de cada paso.
    payment_expires_at: datetime | None = None
    offered_at: datetime | None = None
    accepted_at: datetime | None = None
    paid_at: datetime | None = None
    preparation_started_at: datetime | None = None
    ready_marked_at: datetime | None = None
    delivered_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancel_reason: CancelReason | None = None
    reject_reason: RejectReason | None = None
    out_of_stock_items: list[dict] = field(default_factory=list)  # [{"menu_item_id", "name"}]
    local_date: str | None = None  # fecha de creación en hora de Guatemala (YYYY-MM-DD)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
