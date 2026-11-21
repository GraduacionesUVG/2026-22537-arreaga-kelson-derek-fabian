"""
Rutas de pedidos (Orders) — Módulo C del backend.

Este archivo es EL EJEMPLO DE REFERENCIA del flujo completo de llamadas:

    HTTP request
        -> Router (aquí)
            -> Service (src/services/order_service.py)
                -> Repository (src/repositories/order_repo.py)
                    -> MongoDB (Motor)

Para agregar un nuevo dominio (ej. restaurants), copia esta estructura:
schema -> model -> repository -> service -> router, y regístralo en main.py.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.v1.deps import get_order_actor, get_order_service, require_internal_api_key_strict
from src.schemas.order import (
    OrderCreate,
    OrderRejectRequest,
    OrderResponse,
    OrderStatusResponse,
    OrderStatusUpdate,
    OtpValidateRequest,
    TimeEstimateRequest,
    TimeEstimateResponse,
)
from src.services.payment_service import PaymentGatewayError, PaymentNotConfiguredError
from src.services.restaurant_availability import RestaurantUnavailableError
from src.services.order_service import (
    OrderBelowMinimumError,
    OrderConflictError,
    OrderPermissionError,
    OrderService,
    OrderValidationError,
)

router = APIRouter()


@router.post("/estimate", response_model=TimeEstimateResponse, dependencies=[Depends(require_internal_api_key_strict)])
async def estimate_order_time(data: TimeEstimateRequest, service: OrderService = Depends(get_order_service)):
    """Cálculo de ETC antes de comprometerse a nada (algoritmo ETC v1).

    Pensado para que el chatbot lo llame ANTES de POST /api/orders/, con los
    mismos items que va a pedir, y le muestre al estudiante la hora estimada
    de entrega -- para que tenga chance de cancelar si está muy tardado.

    Si se manda `desired_ready_time`, revisa contra la cola actual del
    restaurante si esa hora alcanza (`feasible`); si no, igual devuelve la
    hora más próxima posible en `ready_at` como alternativa (siempre HOY).

    409 si el restaurante no puede recibir el pedido (`detail.code`:
    kitchen_closed | kitchen_out_of_service | outside_opening_hours). Regla de
    oro: no hay pedidos de un día para otro.

    400 con `detail.code = below_minimum_order` si la suma de productos (sin la
    tarifa de servicio) no llega al mínimo (`MIN_ORDER_AMOUNT`, Q25.00 por
    defecto). `detail.message` ya trae el texto listo para el estudiante, y
    `minimum_amount` / `subtotal_amount` / `missing_amount` los montos.
    """
    try:
        return await service.estimate(data)
    except RestaurantUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.to_detail()) from exc
    except OrderBelowMinimumError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.to_detail()) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post(
    "/",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_api_key_strict)],
)
async def create_order(data: OrderCreate, service: OrderService = Depends(get_order_service)):
    """Registra un borrador de pedido (Fase III, paso 18).

    Nota: en el flujo real este registro lo dispara el orquestador LLM
    (agent/tools.py -> registrar_pedido) a través del webhook de WhatsApp,
    no directamente el estudiante. Este endpoint queda expuesto para poder
    probar el flujo de Mongo sin depender del agente conversacional.

    Todos los `items[].product_name` deben pertenecer al catálogo de
    `restaurant_id` (un pedido no puede mezclar restaurantes) — 400 si no.
    `requested_ready_time` es opcional (ver POST /api/orders/estimate).
    Mismo 409 que /estimate si el restaurante está cerrado o la hora no es de hoy,
    y mismo 400 `below_minimum_order` si no llega al pedido mínimo.
    """
    try:
        return await service.create_order(data)
    except RestaurantUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.to_detail()) from exc
    except OrderBelowMinimumError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.to_detail()) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/active", response_model=list[OrderResponse])
async def get_active_orders(
    actor: dict = Depends(get_order_actor),
    service: OrderService = Depends(get_order_service),
):
    """Cola visible en el KDS: solicitudes ofrecidas a la estación del token y órdenes asignadas a ella.

    Con token de administración devuelve todo lo activo de su restaurante.
    """
    return await service.get_active_orders(actor)


@router.get(
    "/{order_id}/status", response_model=OrderStatusResponse, dependencies=[Depends(require_internal_api_key_strict)]
)
async def get_order_status(order_id: str, service: OrderService = Depends(get_order_service)):
    """Consulta de solo lectura pensada para el bot:
    el estudiante pregunta "¿cómo va mi pedido?" y esto es lo único que el
    orquestador LLM puede llamar -- nunca PUT /{order_id}/status, que es
    exclusivo de KDS/administración.
    """
    order = await service.get_order_status(order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pedido no encontrado.")
    return order


@router.put("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: str,
    data: OrderStatusUpdate,
    actor: dict = Depends(get_order_actor),
    service: OrderService = Depends(get_order_service),
):
    """Módulo C: PUT /api/orders/{id}/status — SOLO KDS/administración, nunca el bot.

    No acepta `accepted` con pago ya confirmado como destino manual: eso solo
    se alcanza al confirmarse el pago (webhook de Tilopay de la cuenta del
    restaurante, o POST /webhooks/bac-payment simulado si el restaurante no
    tiene cuenta y los pagos simulados están permitidos).
    Al aceptar: 502 si Tilopay no pudo generar el link; 409 si el restaurante no tiene cuenta de
    Tilopay configurada (en PRODUCTION). En ambos casos la solicitud sigue pendiente. Tampoco permite cancelar un pedido ya pagado, ni pasar a
    `in_preparation` sin pago confirmado (ver OrderService.update_status).
    """
    try:
        updated = await service.update_status(order_id, data.pipeline_status, actor)
    except OrderPermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except PaymentNotConfiguredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PaymentGatewayError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except (OrderConflictError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Pedido no encontrado.")
    return updated


@router.post("/{order_id}/reject", response_model=OrderResponse)
async def reject_order(
    order_id: str,
    data: OrderRejectRequest,
    actor: dict = Depends(get_order_actor),
    service: OrderService = Depends(get_order_service),
):
    """La cocina rechaza una solicitud pendiente indicando el motivo (SOLO KDS/administración).

    - `kitchen_load`: pasa a la siguiente estación; si ya no queda ninguna, se rechaza
      y el estudiante recibe por WhatsApp el motivo con un "intenta más tarde".
    - `out_of_stock`: requiere `out_of_stock_item_ids` (productos del pedido). El
      rechazo es definitivo, esos productos se desactivan del menú por hoy (vuelven
      solos al día siguiente) y el estudiante recibe los nombres de lo que ya no hay.
    """
    try:
        updated = await service.reject_order(order_id, data.reason, data.out_of_stock_item_ids, actor)
    except OrderPermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except OrderValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except (OrderConflictError, ValueError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Pedido no encontrado.")
    return updated


@router.post("/{order_id}/validate-otp")
async def validate_otp(
    order_id: str,
    data: OtpValidateRequest,
    actor: dict = Depends(get_order_actor),
    service: OrderService = Depends(get_order_service),
):
    """Entrega en mostrador: valida el OTP y marca la orden como entregada."""
    try:
        is_valid = await service.validate_otp(order_id, data.otp_code, actor)
    except OrderPermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    if not is_valid:
        raise HTTPException(status_code=400, detail="OTP inválido o pedido no encontrado.")
    return {"detail": "Pedido marcado como entregado."}
