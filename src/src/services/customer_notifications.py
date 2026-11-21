"""
Aviso por WhatsApp al estudiante en cada evento de pedido que le importa
(aceptado con link de pago, pagado, listo, rechazado, entregado, pago fallido/vencido).

Lógica compartida por los dos caminos que pueden mandar el aviso:

- Con Kafka: src/worker.py consume `orders.events` y llama a notify_customer.
- Sin Kafka (despliegue actual en Render): EventPublisher la llama en una
  tarea de fondo dentro del mismo proceso del API (fallback en proceso),
  para que el estudiante reciba el aviso igual.

Nunca lanza por un envío fallido: WhatsAppService ya lo registra y lo traga.
"""

import logging
from datetime import datetime

from src.configuration import settings
from src.core.database import mongo
from src.integrations.events import record_order_event
from src.repositories.restaurant_repo import RestaurantRepository
from src.repositories.user_repo import UserRepository
from src.services import notification_templates
from src.services.whatsapp_service import WhatsAppService

logger = logging.getLogger("customer_notifications")

# Qué plantilla usar por cada tipo de evento -- "order_created" a propósito
# no está acá: le importa al KDS (ya lo recibe por WebSocket), no es algo
# que el estudiante necesite que le avisen por WhatsApp.
TEMPLATES = {
    "order_accepted": notification_templates.order_accepted_message,
    "order_paid": notification_templates.order_paid_message,
    "order_ready": notification_templates.order_ready_message,
    "order_rejected": notification_templates.order_rejected_message,
    "order_delivered": notification_templates.order_delivered_message,
    "order_payment_failed": notification_templates.order_payment_failed_message,
    "payment_link_expired": notification_templates.order_payment_expired_message,
}

# Nombre legible del aviso para el registro `customer_notified`.
NOTIFICATION_LABELS = {
    "order_accepted": "link de pago",
    "order_paid": "pago confirmado",
    "order_ready": "orden lista",
    "order_rejected": "solicitud rechazada",
    "order_delivered": "pedido entregado",
    "order_payment_failed": "pago no confirmado",
    "payment_link_expired": "link de pago vencido",
}


def should_notify(event_type: str | None) -> bool:
    return event_type in TEMPLATES


async def resolve_whatsapp_number(user_repo: UserRepository, order: dict) -> str | None:
    """`orders.student_id` no está garantizado a corresponder a un usuario
    verificado -- si no
    se encuentra, se registra y se descarta el mensaje en vez de fallar."""
    student_id = order.get("student_id")
    if not student_id:
        return None
    user = await user_repo.find_by_student_id(student_id)
    if user is None:
        logger.warning(
            "Pedido %s: student_id '%s' no corresponde a ningún usuario verificado, no se envía WhatsApp.",
            order.get("id"),
            student_id,
        )
        return None
    return user.get("whatsapp_number")


def _preview_ready_at(payload: dict) -> datetime | None:
    raw = payload.get("preview_ready_at")
    return raw if isinstance(raw, datetime) or raw is None else datetime.fromisoformat(str(raw))


def build_template(payload: dict) -> notification_templates.TemplateMessage | None:
    """Plantilla con botón de URL para los eventos que llevan link (pago,
    calificar); `None` para el resto, que salen siempre como texto."""
    event_type = payload.get("event")
    order = payload.get("order") or {}
    if event_type == "order_accepted":
        return notification_templates.order_accepted_template(order, _preview_ready_at(payload))
    if event_type == "order_delivered":
        return notification_templates.order_delivered_template(order)
    return None


async def _send(whatsapp_service: WhatsAppService, to: str, payload: dict, text: str) -> bool:
    """Plantilla si están activadas y el evento tiene una; si Meta la rechaza
    (no aprobada, parámetros mal) se manda el texto de respaldo."""
    template = build_template(payload) if settings.whatsapp.templates_enabled else None
    if template is not None:
        sent = await whatsapp_service.send_template_message(
            to,
            template.name,
            settings.whatsapp.template_language,
            template.body_params,
            template.url_button_param,
        )
        if sent:
            print(f"[notify] {payload.get('event')} del pedido {(payload.get('order') or {}).get('id')}: "
                  f"Meta aceptó la plantilla '{template.name}'")
            return True
        logger.warning("Plantilla '%s' no salió, se manda el texto de respaldo.", template.name)
    sent_text = await whatsapp_service.send_text_message(to, text)
    order_id = (payload.get("order") or {}).get("id")
    if sent_text:
        print(f"[notify] {payload.get('event')} del pedido {order_id}: Meta aceptó el texto")
    else:
        print(f"[notify] {payload.get('event')} del pedido {order_id}: NO SALIÓ (ni plantilla ni texto)")
    return sent_text


async def _with_restaurant(order: dict) -> dict:
    """Copia del pedido con el nombre y la ubicación del restaurante (el aviso de
    "listo" dice dónde recoger). Si no se encuentra, el aviso sale sin dirección."""
    try:
        restaurant = await RestaurantRepository(mongo.db).find_by_id(order.get("restaurant_id"))
    except Exception:
        logger.warning("Pedido %s: no se pudo leer el restaurante para el aviso.", order.get("id"))
        return order
    if not restaurant:
        return order
    return {
        **order,
        "restaurant_name": restaurant.get("name"),
        "restaurant_location": restaurant.get("location"),
    }


def build_message(payload: dict) -> str | None:
    event_type = payload.get("event")
    template = TEMPLATES.get(event_type)
    if template is None:
        return None
    order = payload.get("order") or {}
    if event_type == "order_accepted":
        return template(order, _preview_ready_at(payload), order.get("payment_reference", ""))
    return template(order)


async def notify_customer(
    payload: dict, user_repo: UserRepository, whatsapp_service: WhatsAppService
) -> dict | None:
    """Manda el WhatsApp del evento y registra `customer_notified` en `order_events`.

    Devuelve el registro `customer_notified` (para difundirlo en vivo), o
    `None` si el evento no lleva aviso o no hay número al cual mandarlo.
    """
    if payload.get("event") == "order_ready":
        payload = {**payload, "order": await _with_restaurant(payload.get("order") or {})}
    text = build_message(payload)
    if text is None:
        return None
    order = payload.get("order") or {}
    to = await resolve_whatsapp_number(user_repo, order)
    if not to:
        print(f"[notify] {payload.get('event')} del pedido {order.get('id')}: sin número de WhatsApp para "
              f"student_id={order.get('student_id')!r}, no se envía")
        return None

    if not await _send(whatsapp_service, to, payload, text):
        return None  # ya quedó en el log; no se registra un aviso que no salió

    event_type = payload.get("event")
    try:
        return await record_order_event(
            "customer_notified", order, extra={"notification": NOTIFICATION_LABELS.get(event_type, event_type)}
        )
    except Exception:
        logger.exception("No se pudo registrar customer_notified del pedido %s.", order.get("id"))
        return None
