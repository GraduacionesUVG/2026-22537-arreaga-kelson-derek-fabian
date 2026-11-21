"""
Esquemas de datos (Pydantic) para las herramientas (tools) que el
Orquestador LLM puede invocar.

Estos modelos cumplen dos roles, tal como describe el protocolo (sección 6.3.3):
1. Generan automáticamente el JSON Schema que se envía a OpenAI para que el
   modelo sepa qué funciones existen y qué parámetros esperan.
2. Validan en tiempo de ejecución la respuesta del modelo (Type Safety /
   Runtime Validation) antes de tocar la base de datos o el KDS.

A medida que definas más funciones del backend (colas, pagos, etc.) agrega
aquí sus modelos correspondientes.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema


class ItemPedido(BaseModel):
    """Un ítem individual dentro de un pedido."""
    nombre_producto: str = Field(description="Nombre exacto del producto tal como aparece en el menú.")
    cantidad: int = Field(
        ge=1,
        description="Cantidad solicitada del producto, ya con las correcciones. 'Un par' = 2, "
        "'media docena' = 6, 'una docena' = 12.",
    )
    modificadores: Optional[list[str]] = Field(
        default=None,
        description="Todo lo que el usuario pidió sobre cómo preparar o servir ESTE producto, con sus palabras, "
        "ej. ['sin queso', 'papas grandes']. Inclúyelo siempre; nunca lo omitas ni lo conviertas en otro producto.",
    )
    notas: Optional[str] = Field(default=None, description="Notas adicionales del cliente.")


class ListarRestaurantesParams(BaseModel):
    """Sin parámetros: siempre devuelve los restaurantes activos, con su horario de hoy ya resuelto."""


class VerMenuParams(BaseModel):
    """Parámetros para consultar el menú de un restaurante específico."""
    restaurant_id: str = Field(description="Id del restaurante, obtenido de listar_restaurantes.")


class EstimarPedidoParams(BaseModel):
    """Parámetros para calcular el tiempo estimado de un pedido ANTES de registrarlo de verdad."""
    restaurant_id: str = Field(description="Id del restaurante donde se va a pedir.")
    items: list[ItemPedido] = Field(description="Productos que llevaría el pedido.")
    hora_deseada: Optional[datetime] = Field(
        default=None,
        description="SOLO si el estudiante pidió expresamente una hora (ej. 'para la 1:00 pm'): esa hora "
        "de HOY en hora de Guatemala, formato ISO sin zona. Si no pidió ninguna hora, NO envíes este "
        "campo (déjalo vacío): el sistema calcula lo antes posible. Nunca inventes una hora ni una fecha.",
    )


class CrearPedidoParams(BaseModel):
    """Parámetros para registrar un pedido de verdad (Estado: Pendiente Aceptación).

    Llamar solo después de que el estudiante confirmó, idealmente tras
    mostrarle el resultado de estimar_pedido con el mismo restaurant_id/items.
    """
    id_estudiante: str = Field(description="Identificador/carné del estudiante que realiza el pedido.")
    restaurant_id: str = Field(description="Id del restaurante (el mismo usado en estimar_pedido).")
    items: list[ItemPedido] = Field(description="Lista de productos que conforman el pedido.")
    hora_deseada: Optional[datetime] = Field(
        default=None,
        description="SOLO si el estudiante pidió expresamente una hora: esa hora de HOY en hora de Guatemala, "
        "formato ISO sin zona. Si no pidió ninguna, NO envíes este campo. Nunca inventes una hora ni una fecha.",
    )


class ConsultarEstadoPedidoParams(BaseModel):
    """Parámetros para consultar (solo lectura) el avance de un pedido ya creado."""
    order_id: str = Field(description="Id del pedido, devuelto por crear_pedido.")


class VerHistorialPedidosParams(BaseModel):
    """Sin parámetros: devuelve el historial del estudiante con el que se está hablando."""
    id_estudiante: SkipJsonSchema[Optional[str]] = None


class BorrarMisDatosParams(BaseModel):
    """Borra lo que el bot recuerda del estudiante (preferencias y conversaciones; no sus pedidos)."""
    confirmado: bool = Field(
        description="true SOLO si el estudiante ya confirmó explícitamente que quiere borrar sus datos."
    )
    id_estudiante: SkipJsonSchema[Optional[str]] = None
