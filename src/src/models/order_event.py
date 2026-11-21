"""
Registro persistente de eventos de pedidos (`order_events`).

Cada cambio relevante de una solicitud/orden queda guardado con un texto
legible (`message`). Alimenta el log en vivo del panel, el historial por día,
la línea de tiempo de una orden y los KPIs.

Ejemplo:
{
    "_id": ObjectId("..."),
    "seq": 58231,
    "event": "order_paid",
    "restaurant_id": "665f1c...",
    "kds_user_ids": ["66a0..."],
    "order_id": "66b1...",
    "request_number": 1011,
    "order_number": 2054,
    "actor": {"type": "system", "id": null, "username": null},
    "message": "Solicitud #1011 pagada y Orden #2054 generada: preparación 11:40, entrega 12:00",
    "data": {"total_amount": 35.0},
    "created_at": ISODate("..."),
    "local_date": "2026-09-25"
}
"""

from enum import Enum

from src.core.timeutils import format_local_hhmm

COLLECTION_NAME = "order_events"


class OrderEventType(str, Enum):
    ORDER_CREATED = "order_created"
    ORDER_OFFERED = "order_offered"
    ORDER_REJECTED_BY_STATION = "order_rejected_by_station"
    ORDER_REJECTED = "order_rejected"
    ORDER_ACCEPTED = "order_accepted"
    PAYMENT_LINK_SENT = "payment_link_sent"
    ORDER_PAID = "order_paid"
    ORDER_IN_PREPARATION = "order_in_preparation"
    ORDER_READY = "order_ready"
    CUSTOMER_NOTIFIED = "customer_notified"
    OTP_FAILED = "otp_failed"
    ORDER_DELIVERED = "order_delivered"
    ORDER_PAYMENT_FAILED = "order_payment_failed"
    PAYMENT_LINK_EXPIRED = "payment_link_expired"
    ORDER_CANCELLED = "order_cancelled"


def describe_items(order: dict) -> str:
    parts = []
    for item in order.get("items", []):
        qty = item.get("quantity", 1)
        name = item.get("product_name", "")
        parts.append(f"{qty}x {name}" if qty != 1 else name)
    return " + ".join(parts) if parts else "sin productos"


def _ref(order: dict) -> str:
    if order.get("order_number"):
        return f"Orden #{order['order_number']}"
    return f"Solicitud #{order.get('request_number', '?')}"


def _reject_detail(order: dict, extra: dict) -> str:
    reason = extra.get("reject_reason") or order.get("reject_reason")
    if reason == "no_response":
        return " (el restaurante no respondió a tiempo)"
    if reason == "kitchen_load":
        return " (carga en cocina)"
    if reason == "out_of_stock":
        names = ", ".join(i.get("name", "") for i in order.get("out_of_stock_items") or [])
        return f" (sin stock: {names})" if names else " (sin stock)"
    return ""


def describe_event(event: str, order: dict, extra: dict | None = None, actor: dict | None = None) -> str:
    """Texto en español para el log del panel de administración."""
    extra = extra or {}
    station = (actor or {}).get("username") or extra.get("station_username")
    by_station = f" por KDS {station}" if station else ""
    request = f"Solicitud #{order.get('request_number', '?')}"
    total = f"Q{order.get('total_amount', 0):.2f}"

    if event == OrderEventType.ORDER_CREATED.value:
        target = extra.get("station_username")
        suffix = f" (enviada a KDS {target})" if target else ""
        return f"{request}: {describe_items(order)} por {total}{suffix}"
    if event == OrderEventType.ORDER_OFFERED.value:
        target = extra.get("station_username")
        return f"{request} enviada a KDS {target}" if target else f"{request} enviada a cocina"
    if event == OrderEventType.ORDER_REJECTED_BY_STATION.value:
        nxt = extra.get("next_station_username")
        return f"{request} rechazada{by_station}{_reject_detail(order, extra)}; se envía a KDS {nxt}"
    if event == OrderEventType.ORDER_REJECTED.value:
        return f"{request} rechazada{by_station}{_reject_detail(order, extra)}"
    if event == OrderEventType.ORDER_ACCEPTED.value:
        return f"{request} aceptada{by_station}"
    if event == OrderEventType.PAYMENT_LINK_SENT.value:
        deadline = format_local_hhmm(order.get("payment_expires_at"))
        return f"Enviado link de pago para {request} (vence {deadline})"
    if event == OrderEventType.ORDER_PAID.value:
        return (
            f"{request} pagada y Orden #{order.get('order_number', '?')} generada: "
            f"preparación {format_local_hhmm(order.get('prep_start_time'))}, "
            f"entrega {format_local_hhmm(order.get('ready_at'))}"
        )
    if event == OrderEventType.ORDER_IN_PREPARATION.value:
        return f"{_ref(order)} en preparación{by_station}"
    if event == OrderEventType.ORDER_READY.value:
        return f"{_ref(order)} preparada{by_station}"
    if event == OrderEventType.CUSTOMER_NOTIFIED.value:
        return f"Se notificó al cliente ({extra.get('notification', 'aviso')}) de {_ref(order)}"
    if event == OrderEventType.OTP_FAILED.value:
        return f"OTP incorrecto para {_ref(order)}"
    if event == OrderEventType.ORDER_DELIVERED.value:
        return f"{_ref(order)} entregada (OTP correcto)"
    if event == OrderEventType.ORDER_PAYMENT_FAILED.value:
        return f"{request} cancelada: pago no confirmado"
    if event == OrderEventType.PAYMENT_LINK_EXPIRED.value:
        return f"{request} cancelada: el link de pago venció"
    if event == OrderEventType.ORDER_CANCELLED.value:
        return f"{request} cancelada{by_station}"
    return f"{_ref(order)}: {event}"
