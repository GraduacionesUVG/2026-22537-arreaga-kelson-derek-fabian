"""
Puente hacia el módulo `agent/` (orquestador LLM + tool calling), que vive
en la raíz del proyecto y NO sigue la arquitectura en capas de `src/`
porque lo mantiene otro integrante del equipo de forma independiente.

IMPORTANTE: este archivo NO modifica nada dentro de `agent/`. Solo lo
importa dinámicamente para conectarlo con el resto del backend.

## Por qué un bridge y no un import directo

- `agent/` no es un paquete Python (no tiene `__init__.py`) y sus módulos
  usan imports planos (`from orchestator import get_session`), asumiendo
  que se ejecutan con `agent/` como directorio de trabajo/raíz de imports.
- Para no tocar ni reestructurar el código de `agent/` (indicación
  explícita del equipo), este archivo agrega la carpeta a `sys.path` en
  tiempo de ejecución e importa sus módulos dinámicamente, dentro de la
  función (import perezoso) para no romper el arranque del resto de la API
  si `agent/` falla al importarse (ej. falta OPENAI_API_KEY).

Cuando el webhook de WhatsApp (src/api/v1/routes/webhooks.py) reciba un
mensaje, debe llamar a `get_agent_response(...)` en vez de importar
`agent/` directamente, para mantener el acoplamiento en un solo lugar.

## TODO (integración real, para coordinar con el dueño de agent/)

1. Reemplazar el manejo de sesiones en memoria de `agent/orchestator.py`
   (diccionario módulo-level `_sessions`) por persistencia en MongoDB
   (colección `conversations`, por ejemplo vía un nuevo repositorio
   `src/repositories/conversation_repo.py`), para sobrevivir reinicios del
   contenedor y escalar a más de un worker/réplica.
2. Conectar `agent/tools.py` (actualmente con datos de prueba hardcodeados
   en `consultar_menu`, `registrar_pedido`, `consultar_estado_cola`) a los
   repositorios reales de `src/repositories/` (`menu_repo.py`,
   `order_repo.py`) en lugar de los stubs.
3. Resolver la duplicación `agent/orchestator.py` / `agent/orquestator.py`
   (el segundo nombre, con "q", parece una copia de depuración con las
   tools desactivadas) con el dueño de esa carpeta antes de depender de un
   solo módulo en producción. Este bridge asume `orchestator.py` (el que
   también usa `agent/main.py`).
4. Decidir dónde vive la transcripción de audio (`agent/transcription.py`):
   lo natural es que `webhooks.py` la invoke antes de llamar a
   `get_agent_response`, pasando ya el texto transcrito.
"""

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


def get_agent_response(user_id: str, message_text: str) -> str:
    """Envía un mensaje ya transcrito (si venía de audio) al orquestador LLM
    de `agent/` y devuelve el texto de respuesta, listo para reenviar por
    WhatsApp.
    """
    from orchestator import get_session  # type: ignore  # módulo plano en agent/, ver docstring del módulo

    session = get_session(user_id)
    session.add_user_message(message_text)
    return session.run_turn()
