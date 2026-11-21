"""
Schemas de entrada/salida de la API para el dominio de pedidos (Orders).

Estos modelos son la "puerta" pública de la API: validan lo que entra en un
request y dan forma a lo que sale en la respuesta. Son distintos de
src/models/order.py, que describe el documento tal como se guarda en Mongo.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from src.core.timeutils import LocalDateTime, format_local_12h
from src.models.category_icons import suggest_icon
from src.models.order import (
    OrderPaymentStatus,
    OrderPipelineStatus,
    RejectReason,
    describe_order_status,
    describe_price_breakdown,
    is_active_pipeline_status,
)


class OrderItemIn(BaseModel):
    product_name: str = Field(description="Nombre exacto del producto en el menú.")
    quantity: int = Field(ge=1)
    modifiers: list[str] = Field(default_factory=list, description="Nombres de modificadores del producto.")
    notes: str | None = None


class OrderCreate(BaseModel):
    student_id: str
    restaurant_id: str
    items: list[OrderItemIn]
    requested_ready_time: datetime | None = Field(
        default=None,
        description="Hora a la que el estudiante quiere el pedido listo. Si no se manda, es 'lo antes posible'. "
        "Se recomienda llamar antes a POST /api/orders/estimate con la misma hora para saber si alcanza.",
    )


class OrderModifierOut(BaseModel):
    name: str
    price: float = 0.0
    custom: bool = False


class OrderItemOut(BaseModel):
    product_name: str
    quantity: int
    modifiers: list[OrderModifierOut] = Field(default_factory=list)
    notes: str | None = None
    unit_price: float = 0.0
    modifiers_total: float = 0.0
    line_total: float = 0.0
    prep_time_minutes: float = 0.0
    menu_item_id: str | None = None
    category: str | None = None
    category_icon: str | None = None
    ready_from: str | None = None

    @field_validator("modifiers", mode="before")
    @classmethod
    def _legacy_modifiers(cls, value):
        return [{"name": v, "price": 0.0, "custom": True} if isinstance(v, str) else v for v in value or []]

    @model_validator(mode="after")
    def _legacy_category_icon(self):
        # Pedidos anteriores a los iconos de categoría: se sugiere uno según el nombre.
        if self.category_icon is None and self.category:
            self.category_icon = suggest_icon(self.category)
        return self


class OutOfStockItemOut(BaseModel):
    menu_item_id: str
    name: str


class OrderResponse(BaseModel):
    id: str
    student_id: str
    restaurant_id: str
    items: list[OrderItemOut]
    pipeline_status: OrderPipelineStatus = Field(description="Progreso físico del pedido (cocina/mostrador).")
    payment_status: OrderPaymentStatus = Field(description="Estado del cobro, independiente del pipeline.")
    subtotal_amount: float | None = Field(default=None, description="Suma de los productos (sin tarifa de servicio).")
    service_fee: float = Field(default=0.0, description="Tarifa de servicio del restaurante cobrada en este pedido (Q).")
    total_amount: float = Field(default=0.0, description="Lo que paga el cliente: subtotal_amount + service_fee.")
    requested_ready_time: LocalDateTime | None = None
    prep_start_time: LocalDateTime | None = None
    ready_at: LocalDateTime | None = None
    otp_code: str | None = None
    payment_reference: str | None = None
    request_number: int | None = None
    order_number: int | None = None
    offered_kds_id: str | None = None
    assigned_kds_id: str | None = None
    declined_kds_ids: list[str] = Field(default_factory=list)
    payment_expires_at: LocalDateTime | None = None
    accepted_at: LocalDateTime | None = None
    paid_at: LocalDateTime | None = None
    preparation_started_at: LocalDateTime | None = None
    ready_marked_at: LocalDateTime | None = None
    delivered_at: LocalDateTime | None = None
    cancelled_at: LocalDateTime | None = None
    cancel_reason: str | None = None
    reject_reason: str | None = Field(default=None, description="kitchen_load | out_of_stock (si la cocina la rechazó).")
    out_of_stock_items: list[OutOfStockItemOut] = Field(default_factory=list)
    local_date: str | None = None
    created_at: LocalDateTime
    updated_at: LocalDateTime

    @model_validator(mode="after")
    def _legacy_subtotal(self):
        # Pedidos anteriores a la tarifa de servicio: no tienen subtotal guardado.
        if self.subtotal_amount is None:
            self.subtotal_amount = round(self.total_amount - self.service_fee, 2)
        return self

    @computed_field
    @property
    def price_breakdown(self) -> str:
        """"Subtotal Q60.00 + Tarifa de servicio Q2.65 = Total Q62.65" (texto para el bot/cliente)."""
        return describe_price_breakdown(self.subtotal_amount, self.service_fee, self.total_amount)

    @computed_field
    @property
    def active(self) -> bool:
        """True mientras el pedido siga "vivo" (no entregado/rechazado/cancelado)."""
        return is_active_pipeline_status(self.pipeline_status)

    @computed_field
    @property
    def ready_at_guatemala(self) -> str | None:
        """`ready_at` ya en hora de Guatemala, legible ("2:05 pm")."""
        return format_local_12h(self.ready_at)

    @computed_field
    @property
    def payment_expires_at_guatemala(self) -> str | None:
        """`payment_expires_at` ya en hora de Guatemala, legible ("1:40 pm")."""
        return format_local_12h(self.payment_expires_at)

    @computed_field
    @property
    def status_label(self) -> str:
        """Etiqueta en español lista para mostrarle al estudiante, sin que el bot
        tenga que traducir la combinación pipeline_status + payment_status."""
        return describe_order_status(self.pipeline_status, self.payment_status)


class TimeEstimateRequest(BaseModel):
    """Input de POST /api/orders/estimate: mismos items/restaurante que un OrderCreate, sin crear nada todavía."""

    restaurant_id: str
    items: list[OrderItemIn]
    desired_ready_time: datetime | None = Field(
        default=None, description="Hora a la que se quisiera tener el pedido. Si no se manda, se calcula ASAP."
    )


class TimeEstimateResponse(BaseModel):
    restaurant_id: str
    subtotal_amount: float | None = Field(default=None, description="Suma de los productos (sin tarifa de servicio).")
    service_fee: float = Field(default=0.0, description="Tarifa de servicio del restaurante (Q por pedido).")
    total_amount: float = Field(description="Lo que pagaría el cliente: subtotal_amount + service_fee.")
    order_prep_minutes: float = Field(description="Tiempo de preparación del pedido completo (items en paralelo).")
    queue_backlog_minutes: float = Field(description="Minutos que la cocina tiene comprometidos ahora mismo.")
    prep_start_time: LocalDateTime = Field(
        description="Hora (Guatemala, -06:00) a la que el KDS debería empezar a prepararlo."
    )
    ready_at: LocalDateTime = Field(description="Hora estimada (Guatemala, -06:00) en que el pedido estaría listo.")
    feasible: bool = Field(
        description="Si se mandó desired_ready_time: si alcanza el tiempo. Si no se mandó: siempre true."
    )
    notices: list[str] = Field(default_factory=list, description="Avisos para el estudiante (ej. 'listo aprox. desde').")

    @model_validator(mode="after")
    def _default_subtotal(self):
        if self.subtotal_amount is None:
            self.subtotal_amount = round(self.total_amount - self.service_fee, 2)
        return self

    @computed_field
    @property
    def price_breakdown(self) -> str:
        """"Subtotal Q60.00 + Tarifa de servicio Q2.65 = Total Q62.65" -- el bot debe mostrarlo al confirmar."""
        return describe_price_breakdown(self.subtotal_amount, self.service_fee, self.total_amount)

    @computed_field
    @property
    def ready_at_guatemala(self) -> str | None:
        """`ready_at` ya en hora de Guatemala, legible ("2:05 pm")."""
        return format_local_12h(self.ready_at)


class OrderStatusUpdate(BaseModel):
    """Body de PUT /api/orders/{id}/status -- SOLO para KDS/administración.

    El bot NUNCA debe recibir esta operación como tool:
    únicamente puede consultar, no mutar, el estado de un pedido.
    """

    pipeline_status: OrderPipelineStatus


class OrderRejectRequest(BaseModel):
    """Body de POST /api/orders/{id}/reject -- rechazo de una solicitud desde el KDS."""

    reason: RejectReason = Field(description="kitchen_load (carga en cocina) u out_of_stock (falta de stock).")
    out_of_stock_item_ids: list[str] = Field(
        default_factory=list,
        description="Solo con out_of_stock: `menu_item_id` de los productos del pedido que no hay. "
        "Se desactivan del menú por el resto del día.",
    )

    @model_validator(mode="after")
    def _check(self):
        if self.reason == RejectReason.OUT_OF_STOCK:
            self.out_of_stock_item_ids = list(dict.fromkeys(self.out_of_stock_item_ids))
            if not self.out_of_stock_item_ids:
                raise ValueError("Con out_of_stock se debe enviar al menos un item en out_of_stock_item_ids.")
        elif self.out_of_stock_item_ids:
            raise ValueError("out_of_stock_item_ids solo se envía con reason = out_of_stock.")
        return self


class OtpValidateRequest(BaseModel):
    otp_code: str


class SimulatedPaymentWebhookIn(BaseModel):
    """Payload de POST /webhooks/bac-payment mientras no hay ambiente sandbox de BAC real."""

    order_id: str
    status: Literal["success", "failed"] = "success"


class OrderStatusResponse(BaseModel):
    """GET /api/orders/{id}/status -- versión de solo lectura pensada para el bot.

    A propósito NO incluye los items ni el total: el bot ya los conoce (los
    mandó él al crear el pedido), y este endpoint es solo para "¿cómo va mi pedido?".
    """

    id: str
    pipeline_status: OrderPipelineStatus
    payment_status: OrderPaymentStatus
    ready_at: LocalDateTime | None = None
    request_number: int | None = None
    order_number: int | None = None
    payment_expires_at: LocalDateTime | None = Field(
        default=None, description="Hora límite para pagar (si aplica), en hora de Guatemala."
    )
    otp_code: str | None = Field(
        default=None, description="Código para recoger en mostrador, solo presente desde 'accepted'+pagado en adelante."
    )

    @computed_field
    @property
    def active(self) -> bool:
        return is_active_pipeline_status(self.pipeline_status)

    @computed_field
    @property
    def ready_at_guatemala(self) -> str | None:
        """`ready_at` ya en hora de Guatemala, legible ("2:05 pm")."""
        return format_local_12h(self.ready_at)

    @computed_field
    @property
    def payment_expires_at_guatemala(self) -> str | None:
        """`payment_expires_at` ya en hora de Guatemala, legible ("1:40 pm")."""
        return format_local_12h(self.payment_expires_at)

    @computed_field
    @property
    def status_label(self) -> str:
        return describe_order_status(self.pipeline_status, self.payment_status)
