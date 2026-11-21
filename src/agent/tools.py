"""
Registro de herramientas (tools) disponibles para el Orquestador LLM.

Cada tool tiene:
- Un esquema (derivado automáticamente del modelo Pydantic) que se le describe al modelo.
- Una función Python real que se ejecuta cuando el modelo decide invocarla.

Las tools llaman al backend real (GP-Agent-Backend) vía backend_client.py (en la raíz del repo) para el detalle de cada
endpoint, el mapeo de campos español/inglés y qué NO debe exponerse como tool
(cambiar el estado de un pedido es exclusivo del KDS, nunca del bot).
"""

from datetime import datetime

import httpx

from backend_client import BackendError, backend_request, delete_user_memory, get_order_history
from models import (
    BorrarMisDatosParams,
    ConsultarEstadoPedidoParams,
    CrearPedidoParams,
    EstimarPedidoParams,
    ItemPedido,
    ListarRestaurantesParams,
    VerHistorialPedidosParams,
    VerMenuParams,
)


def _pydantic_to_openai_tool(name: str, description: str, model) -> dict:
    """Convierte un modelo Pydantic en la estructura 'tool' que espera la API de OpenAI."""
    schema = model.model_json_schema()
    # OpenAI no necesita el "title" que agrega Pydantic por default
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _item_a_backend(item: ItemPedido) -> dict:
    """Traduce ItemPedido (campos en español) al OrderItemIn que espera el backend (inglés)."""
    return {
        "product_name": item.nombre_producto,
        "quantity": item.cantidad,
        "modifiers": item.modificadores or [],
        "notes": item.notas,
    }


def _hora_iso(hora: datetime | None) -> str | None:
    return hora.isoformat() if hora is not None else None


# TOOLS


def listar_restaurantes(params: ListarRestaurantesParams) -> dict:
    try:
        restaurantes = backend_request("GET", "/api/restaurants/", params={"active_only": True})
        return {"restaurantes": restaurantes}
    except BackendError as exc:
        return {"error": exc.detail}


def ver_menu(params: VerMenuParams) -> dict:
    try:
        items = backend_request("GET", "/api/menus/", params={"restaurant_id": params.restaurant_id})
        return {"items": items}
    except BackendError as exc:
        return {"error": exc.detail}


def estimar_pedido(params: EstimarPedidoParams) -> dict:
    body = {
        "restaurant_id": params.restaurant_id,
        "items": [_item_a_backend(item) for item in params.items],
        "desired_ready_time": _hora_iso(params.hora_deseada),
    }
    try:
        result = backend_request("POST", "/api/orders/estimate", json=body)
    except BackendError as exc:
        return {"error": exc.detail}
    return _con_minutos_despues_del_pago(result)


def _con_minutos_despues_del_pago(result: dict) -> dict:
    """Agrega cuánto tarda DESPUÉS de pagar (cola + preparación). `ready_at` está calculado desde
    AHORA y el estudiante puede tardar en pagar, así que el modelo no debe prometer esa hora/duración."""
    if not isinstance(result, dict) or "order_prep_minutes" not in result:
        return result
    total = float(result.get("order_prep_minutes") or 0) + float(result.get("queue_backlog_minutes") or 0)
    return {**result, "listo_aprox_minutos_despues_del_pago": max(1, round(total))}


def crear_pedido(params: CrearPedidoParams) -> dict:
    body = {
        "student_id": params.id_estudiante,
        "restaurant_id": params.restaurant_id,
        "items": [_item_a_backend(item) for item in params.items],
        "requested_ready_time": _hora_iso(params.hora_deseada),
    }
    try:
        return backend_request("POST", "/api/orders/", json=body)
    except BackendError as exc:
        return {"error": exc.detail}


def consultar_estado_pedido(params: ConsultarEstadoPedidoParams) -> dict:
    try:
        return backend_request("GET", f"/api/orders/{params.order_id}/status")
    except BackendError as exc:
        return {"error": exc.detail}


def ver_historial_pedidos(params: VerHistorialPedidosParams) -> dict:
    try:
        return get_order_history(params.id_estudiante)
    except BackendError as exc:
        return {"error": exc.detail}
    except httpx.HTTPError:
        return {"error": "No se pudo consultar el historial en este momento."}


def borrar_mis_datos(params: BorrarMisDatosParams) -> dict:
    if not params.confirmado:
        return {"error": "Primero pide al estudiante que confirme que quiere borrar sus datos."}
    try:
        delete_user_memory(params.id_estudiante)
    except BackendError as exc:
        return {"error": exc.detail}
    except httpx.HTTPError:
        return {"error": "No se pudieron borrar los datos en este momento, intenta más tarde."}
    return {"ok": True, "detalle": "Se borraron preferencias y conversaciones. Los pedidos se conservan."}


# TOOL REGISTRY

TOOLS_REGISTRY = {
    "listar_restaurantes": {
        "schema": _pydantic_to_openai_tool(
            "listar_restaurantes",
            "Lista los restaurantes activos: nombre, ubicación en el campus y horario de hoy ya resuelto.",
            ListarRestaurantesParams,
        ),
        "model": ListarRestaurantesParams,
        "func": listar_restaurantes,
    },
    "ver_menu": {
        "schema": _pydantic_to_openai_tool(
            "ver_menu",
            "Consulta el menú disponible de un restaurante (ya filtrado al almuerzo del día si aplica).",
            VerMenuParams,
        ),
        "model": VerMenuParams,
        "func": ver_menu,
    },
    "estimar_pedido": {
        "schema": _pydantic_to_openai_tool(
            "estimar_pedido",
            "Calcula el total de un posible pedido ANTES de registrarlo. Llamar siempre antes de "
            "crear_pedido y mostrarle al estudiante productos, restaurante y total para que confirme "
            "en su siguiente mensaje. No prometas hora de entrega: el restaurante la define al aceptar. "
            "Para decir cuánto tarda usa `listo_aprox_minutos_despues_del_pago` (minutos DESPUÉS de pagar).",
            EstimarPedidoParams,
        ),
        "model": EstimarPedidoParams,
        "func": estimar_pedido,
    },
    "crear_pedido": {
        "schema": _pydantic_to_openai_tool(
            "crear_pedido",
            "Envía el pedido al restaurante (queda pendiente de que lo acepte; recién ahí se cobra). "
            "Solo después de que el estudiante confirmó, en un mensaje posterior, lo que mostraste con estimar_pedido.",
            CrearPedidoParams,
        ),
        "model": CrearPedidoParams,
        "func": crear_pedido,
        "needs_student_id": True,
    },
    "consultar_estado_pedido": {
        "schema": _pydantic_to_openai_tool(
            "consultar_estado_pedido",
            "Consulta el estado actual de un pedido ya creado (solo lectura, no lo modifica).",
            ConsultarEstadoPedidoParams,
        ),
        "model": ConsultarEstadoPedidoParams,
        "func": consultar_estado_pedido,
    },
    "ver_historial_pedidos": {
        "schema": _pydantic_to_openai_tool(
            "ver_historial_pedidos",
            "Historial de pedidos del estudiante: lo que más pide, sus últimos pedidos y lo que ya probó. "
            "Úsala para 'lo de siempre', 'lo mismo de la otra vez' o 'algo que no haya pedido'.",
            VerHistorialPedidosParams,
        ),
        "model": VerHistorialPedidosParams,
        "func": ver_historial_pedidos,
        "needs_student_id": True,
    },
    "borrar_mis_datos": {
        "schema": _pydantic_to_openai_tool(
            "borrar_mis_datos",
            "Borra lo que el bot recuerda del estudiante (preferencias y conversaciones, no los pedidos). "
            "Solo si el estudiante lo pidió Y confirmó explícitamente en un mensaje aparte.",
            BorrarMisDatosParams,
        ),
        "model": BorrarMisDatosParams,
        "func": borrar_mis_datos,
        "needs_student_id": True,
    },
}


def get_openai_tools_schema() -> list[dict]:
    """Lista de tools en formato OpenAI, lista para pasar al parámetro `tools=` del chat completion."""
    return [entry["schema"] for entry in TOOLS_REGISTRY.values()]
