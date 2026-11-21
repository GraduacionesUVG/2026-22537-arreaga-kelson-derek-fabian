"""
Webhooks entrantes (Módulo B): WhatsApp (Meta) y pasarela de
pagos Tilopay (más el webhook de pago simulado, solo sin credenciales).

Estos endpoints deben responder rápido (HTTP 200) y delegar el
procesamiento pesado de forma asíncrona. Por ahora se procesan inline; cuando se agregue una
cola real (ej. Redis Streams/Celery/RQ), mover la lógica pesada a un worker.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.api.v1.deps import get_order_service, require_internal_api_key_strict
from src.schemas.order import SimulatedPaymentWebhookIn
from src.services.agent_bridge import get_agent_response
from src.services.order_service import OrderService
from src.services.payment_service import PROVIDER_MOCK, simulated_payments_allowed, verify_order_webhook_token

router = APIRouter()
logger = logging.getLogger("webhooks")


@router.post("/whatsapp", dependencies=[Depends(require_internal_api_key_strict)])
async def whatsapp_webhook(request: Request):
    """POST /webhooks/whatsapp — mensajes/audios entrantes desde Meta.

    TODO:
    1. Parsear el payload real de Meta (distinto para texto/audio/botones).
    2. Si es audio, descargar con src/integrations/whatsapp_client.py y
       transcribir con agent/transcription.py antes de pasar el texto al
       orquestador.
    3. Llamar a get_agent_response(user_id, texto) (ver
       src/services/agent_bridge.py).
    4. Enviar la respuesta con src/services/whatsapp_service.py en vez de
       devolverla directo en el response (Meta espera 200 vacío/rápido).

    El bloque de abajo es un placeholder de "eco" para poder probar el
    endpoint end-to-end sin tener aún el webhook real de Meta conectado.
    """
    payload = await request.json()
    user_id = payload.get("user_id", "usuario-demo")
    message_text = payload.get("message", "")
    if not message_text:
        return {"detail": "Sin mensaje, nada que procesar."}

    reply = get_agent_response(user_id, message_text)
    return {"reply": reply}


@router.post("/bac-payment")
async def bac_payment_webhook(
    payload: SimulatedPaymentWebhookIn,
    service: OrderService = Depends(get_order_service),
):
    """POST /webhooks/bac-payment — confirmación de pago (Fase III, paso 25).

    Solo para restaurantes SIN cuenta de Tilopay y con pagos simulados permitidos
    (fuera de PRODUCTION, o `SIMULATED_PAYMENTS_ENABLED=true`).

    SIMULADO: no hay ambiente sandbox de BAC todavía, así que no se valida
    firma ECC (TODO: src/integrations/bac_client.py::verify_webhook_signature)
    y se confía en el payload tal cual. Para probar el flujo completo,
    llamar a mano con el `order_id` de un pedido `accepted` con pago pendiente:

        POST /webhooks/bac-payment
        {"order_id": "<id>", "status": "success"}

    `status="failed"` cancela el pedido (payment_status -> failed,
    pipeline_status -> cancelled) en vez de dejarlo esperando para siempre.
    """
    if not simulated_payments_allowed():
        raise HTTPException(404, "Pago simulado deshabilitado: los pagos se confirman por Tilopay.")
    try:
        current = await service.repo.find_by_id(payload.order_id)
    except ValueError:
        current = None
    if current is not None and current.get("payment_provider") not in (None, PROVIDER_MOCK):
        # El link es de Tilopay: el pago solo se confirma consultando la cuenta del restaurante.
        raise HTTPException(404, "Pago simulado deshabilitado: este pedido se paga por Tilopay.")
    if payload.status != "success":
        order = await service.cancel_for_nonpayment(payload.order_id)
        if order is None:
            raise HTTPException(404, "Pedido no encontrado o no está esperando pago.")
        return {"detail": "Pago no exitoso, pedido cancelado por falta de pago.", "order": order}

    order = await service.mark_paid_and_queue(payload.order_id)
    if order is None:
        raise HTTPException(404, "Pedido no encontrado o no está esperando pago.")
    return {"detail": "Pago confirmado, pedido en cola de preparación.", "order": order}


# --- Tilopay ----------------------------------------------------------------------------------
#
# Las dos URLs se le pasan a Tilopay al crear el link (PaymentService.request_payment_link)
# y llevan un token HMAC propio del pedido. Lo que Tilopay manda en el cuerpo o en el
# query (`code`, `orderHash`, ...) NO se usa para decidir: solo dispara una consulta a
# Tilopay (OrderService.confirm_gateway_payment). Un pago rechazado no cancela el
# pedido: el cliente puede reintentar hasta que venza el link.


@router.post("/tilopay/{order_id}/{token}")
async def tilopay_webhook(order_id: str, token: str, service: OrderService = Depends(get_order_service)):
    """Aviso servidor-a-servidor de Tilopay tras un intento de pago (un solo envío, sin reintentos)."""
    if not verify_order_webhook_token(order_id, token):
        raise HTTPException(403, "Token inválido.")
    result = await service.confirm_gateway_payment(order_id)
    logger.info("Webhook de Tilopay para %s: %s", order_id, result)
    return {"result": result}


_RETURN_MESSAGES = {
    "paid": ("Pago recibido", "Tu orden ya está en cola. Vuelve a WhatsApp para ver los detalles."),
    "already_paid": ("Pago recibido", "Tu orden ya está en cola. Vuelve a WhatsApp para ver los detalles."),
    "not_paid": ("Pago no completado", "No se registró el pago. Puedes intentarlo de nuevo desde el link que recibiste."),
    "late": ("Pedido vencido", "El tiempo para pagar ya había terminado. Escríbenos por WhatsApp para gestionar tu reembolso."),
    "error": ("Estamos confirmando tu pago", "Te avisaremos por WhatsApp en unos minutos."),
}


@router.get("/tilopay/return/{order_id}/{token}", response_class=HTMLResponse)
async def tilopay_return(order_id: str, token: str, service: OrderService = Depends(get_order_service)):
    """Página a la que Tilopay redirige al cliente después de pagar (respaldo del webhook)."""
    if not verify_order_webhook_token(order_id, token):
        raise HTTPException(403, "Token inválido.")
    result = await service.confirm_gateway_payment(order_id)
    title, body = _RETURN_MESSAGES.get(result, ("Pedido no encontrado", "Vuelve a WhatsApp para continuar."))
    return HTMLResponse(
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:system-ui,sans-serif;text-align:center;padding:3rem 1.5rem'>"
        f"<h1>{title}</h1><p>{body}</p></body></html>"
    )
