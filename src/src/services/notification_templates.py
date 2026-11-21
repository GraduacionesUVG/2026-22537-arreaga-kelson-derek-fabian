"""
Texto de los mensajes que el backend manda por WhatsApp de forma directa
(sin pasar por el orquestador conversacional) en cada paso del pedido que le importa al
estudiante: aceptación (con el link de pago), confirmación de pago, listo
para recoger, y los dos casos en que el pedido no va a llegar (rechazo del
restaurante, pago no confirmado).

Separado de src/services/whatsapp_service.py a propósito: acá solo se arma
el texto (se puede leer/ajustar sin tocar nada de red), el envío real vive
en otro archivo.
"""

from dataclasses import dataclass, field
from datetime import datetime

from src.configuration import settings
from src.models.restaurant import GUATEMALA_TZ


def _format_local_time(dt: datetime | str | None) -> str:
    # Con Kafka, el pedido llega serializado a JSON y las fechas vienen como texto ISO.
    dt = _as_datetime(dt)
    if dt is None:
        return "por confirmar"
    local = dt.astimezone(GUATEMALA_TZ)
    return local.strftime("%I:%M %p").lstrip("0").lower()


def _describe_items(order: dict) -> str:
    parts = []
    for item in order.get("items", []):
        qty = item.get("quantity", 1)
        name = item.get("product_name", "")
        parts.append(f"{qty}x {name}" if qty != 1 else name)
    return ", ".join(parts) if parts else "sin items"


def _as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def order_delivered_message(order: dict) -> str:
    # `RATING_URL` = el formulario de opinión. Sin el link, solo el agradecimiento.
    rating_line = f"\nCuéntanos qué te pareció: {settings.whatsapp.rating_url}" if settings.whatsapp.rating_url else ""
    return f"Pedido entregado. ¡Gracias por tu compra!{rating_line}"


def _amount_lines(order: dict) -> str:
    """"Total a pagar", con el desglose si el restaurante cobra tarifa de servicio."""
    total = float(order.get("total_amount") or 0)
    fee = float(order.get("service_fee") or 0)
    if fee <= 0:
        return f"Total a pagar: Q{total:.2f}\n"
    subtotal = float(order.get("subtotal_amount") if order.get("subtotal_amount") is not None else total - fee)
    return f"Subtotal: Q{subtotal:.2f}\nTarifa de servicio: Q{fee:.2f}\nTotal a pagar: Q{total:.2f}\n"


def order_accepted_message(order: dict, preview_ready_at: datetime | None, payment_link: str) -> str:
    deadline = _as_datetime(order.get("payment_expires_at"))
    deadline_line = f"Paga antes de las {_format_local_time(deadline)}.\n" if deadline else ""
    return (
        f"Tu pedido #{order.get('request_number', '')} fue aceptado.\n"
        f"Pedido: {_describe_items(order)}\n"
        f"Hora estimada de entrega: {_format_local_time(preview_ready_at)}\n"
        f"{_amount_lines(order)}"
        f"{deadline_line}"
        "Si tardas en pagar, la hora de entrega puede variar.\n"
        f"Link de pago: {payment_link}"
    )


def order_paid_message(order: dict) -> str:
    return (
        f"Pago confirmado. Tu orden es la #{order.get('order_number', '')}.\n"
        f"Hora estimada de entrega: {_format_local_time(order.get('ready_at'))}\n"
        f"Código para recoger en mostrador: {order.get('otp_code', '----')}"
    )


def order_ready_message(order: dict) -> str:
    """`restaurant_name` / `restaurant_location` los agrega customer_notifications
    desde el restaurante; sin ellos el aviso sale sin dirección."""
    name = str(order.get("restaurant_name") or "").strip()
    location = str(order.get("restaurant_location") or "").strip()
    where = f" en {name}" if name else ""
    address_line = f"Dónde: {location}\n" if location else ""
    return (
        f"Tu pedido ya está listo para recoger{where}.\n"
        f"{address_line}"
        f"Código para recoger en mostrador: {order.get('otp_code', '----')}"
    )


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " y " + names[-1]


def order_rejected_message(order: dict) -> str:
    reason = order.get("reject_reason")
    if reason == "no_response":
        return (
            "Tu pedido no fue atendido: el restaurante no respondió a tiempo.\n"
            f"Pedido: {_describe_items(order)}\n"
            "Puedes intentarlo de nuevo en un momento."
        )
    if reason == "kitchen_load":
        return (
            "Tu pedido no pudo ser aceptado: la cocina tiene mucha carga en este momento.\n"
            f"Pedido: {_describe_items(order)}\n"
            "Intenta más tarde."
        )
    if reason == "out_of_stock":
        names = [i.get("name", "") for i in order.get("out_of_stock_items") or [] if i.get("name")]
        missing = _join_names(names) if names else "uno de los productos"
        return (
            f"Tu pedido no pudo ser aceptado: por hoy ya no hay {missing}.\n"
            f"Pedido: {_describe_items(order)}\n"
            "Si quieres, puedes hacer un nuevo pedido con otros productos."
        )
    return (
        "Tu pedido no pudo ser aceptado por el restaurante.\n"
        f"Pedido: {_describe_items(order)}\n"
    )


def order_payment_expired_message(order: dict) -> str:
    return (
        "El tiempo para pagar tu pedido venció y se canceló.\n"
        f"Pedido: {_describe_items(order)}\n"
        "Si quieres, puedes volver a hacer el pedido."
    )


def order_payment_failed_message(order: dict) -> str:
    return (
        "No se pudo confirmar el pago de tu pedido y se canceló.\n"
        f"Pedido: {_describe_items(order)}\n"
        "Si quieres, puedes volver a hacer el pedido."
    )


# --- Message Templates (botón de URL en vez del link pegado) ----------------
# Cada función arma los parámetros de la plantilla aprobada en Meta; el texto
# y los botones de la plantilla en sí se definen en Meta Business Manager. Si cambia el orden de {{n}} allá, cambia acá.


@dataclass(frozen=True)
class TemplateMessage:
    name: str
    body_params: list[str] = field(default_factory=list)
    url_button_param: str | None = None


def payment_url_param(order: dict) -> str:
    """Lo que va en el {{1}} del botón dinámico de pago: la parte de la URL
    que viene después de PAYMENT_URL_BASE. Mientras `payment_reference` sea el
    link completo con esa base se recorta; si no (pasarela mock / base sin
    configurar), se usa el id del pedido."""
    reference = str(order.get("payment_reference") or "")
    base = settings.whatsapp.payment_url_base
    if base and reference.startswith(base):
        return reference[len(base):]
    return str(order.get("id", ""))


def order_accepted_template(order: dict, preview_ready_at: datetime | None) -> TemplateMessage:
    """Cuerpo: {{1}} número de solicitud, {{2}} pedido, {{3}} hora estimada,
    {{4}} total, {{5}} hora límite de pago. Botón URL dinámico: pagar."""
    deadline = _as_datetime(order.get("payment_expires_at"))
    return TemplateMessage(
        name=settings.whatsapp.template_payment,
        body_params=[
            str(order.get("request_number", "")),
            _describe_items(order),
            _format_local_time(preview_ready_at),
            f"Q{order.get('total_amount', 0):.2f}",
            _format_local_time(deadline),
        ],
        url_button_param=payment_url_param(order),
    )


def order_delivered_template(order: dict) -> TemplateMessage:
    """Cuerpo sin variables (ej. "Pedido entregado. ¡Gracias por tu compra!"). Botón URL estático
    (formulario de opinión): sin parámetros. Si Meta rechaza la plantilla (p. ej. la anterior,
    que sí llevaba {{1}}), sale el texto de respaldo con el link de `RATING_URL`."""
    return TemplateMessage(name=settings.whatsapp.template_delivered, body_params=[])
