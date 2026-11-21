"""
Forma del documento de Mongo para la colección `conversation_metrics`.

Un documento por pedido creado por el agente de WhatsApp. Sirve para medir el
objetivo "un pedido estándar se procesa en menos de 45 segundos". Solo guarda
tiempos y conteos: no lleva `student_id`, texto de mensajes ni audios.

Ejemplo:
{
    "_id": ObjectId("..."),
    "order_id": "66b1...",
    "first_message_at": ISODate("2026-10-20T15:00:00Z"),
    "order_created_at": ISODate("2026-10-20T15:00:38Z"),
    "duration_s": 38.0,          # tiempo total (reloj de pared): del primer mensaje al pedido creado
    "bot_processing_s": 14.1,    # suma de lo que tardó el sistema (descarga+Whisper, backend, LLM, tools)
    "turns": 2,
    "input_types": ["audio", "text"],
    "turn_log": [
        {"input_type": "audio", "audio_ms": 1900, "llm_ms": 2300, "tools_ms": 400, "handle_ms": 5100,
         "tools": ["listar_restaurantes"]}
    ],
    "local_date": "2026-10-20",
    "created_at": ISODate("...")
}

`duration_s` incluye el tiempo que el estudiante tarda en escribir o hablar;
`bot_processing_s` es lo que controla el sistema. Se reportan los dos.
"""

COLLECTION_NAME = "conversation_metrics"

# Objetivo del proyecto: pedido estándar en menos de este número de segundos.
TARGET_SECONDS = 45.0
