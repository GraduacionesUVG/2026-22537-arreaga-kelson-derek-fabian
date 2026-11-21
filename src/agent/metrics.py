"""
Tiempos de la conversación de un pedido (objetivo: pedido estándar < 45 s).

`OrderMetrics` vive dentro de cada ConversationSession y acumula, por turno,
cuánto tardó cada parte (audio/Whisper, LLM, tools, turno completo). Cuando el
agente crea un pedido, el webhook manda el acumulado al backend
(POST /api/internal/conversation-metrics, ver backend_client.py) y lo reinicia
para medir el siguiente pedido.

Solo guarda tiempos y conteos: ni texto de mensajes, ni audios, ni student_id.
Las métricas son best-effort: si algo falla, el pedido NO se ve afectado.
"""

import time
from datetime import datetime, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def meta_timestamp(value) -> datetime | None:
    """`message["timestamp"]` de Meta (segundos Unix como string) -> datetime UTC."""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class OrderMetrics:
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.first_message_at: datetime | None = None
        self.order_id: str | None = None
        self.order_created_at: datetime | None = None
        self._turns: list[dict] = []
        self._turn: dict | None = None
        self._t0 = 0.0

    # --- Por turno -------------------------------------------------------------

    def begin_turn(self, sent_at: datetime | None, input_type: str, audio_ms: float = 0.0) -> None:
        """`sent_at`: hora a la que el estudiante mandó el mensaje (la de Meta; si no hay, la de ahora)."""
        if self.first_message_at is None:
            self.first_message_at = sent_at or _now()
        self._t0 = time.perf_counter()
        self._turn = {
            "input_type": input_type if input_type in ("text", "audio") else "other",
            "audio_ms": round(audio_ms, 1),
            "llm_ms": 0.0,
            "tools_ms": 0.0,
            "handle_ms": 0.0,
            "tools": [],
        }

    def add_llm(self, ms: float) -> None:
        if self._turn is not None:
            self._turn["llm_ms"] = round(self._turn["llm_ms"] + ms, 1)

    def add_tool(self, name: str, ms: float) -> None:
        if self._turn is not None:
            self._turn["tools_ms"] = round(self._turn["tools_ms"] + ms, 1)
            self._turn["tools"].append(name)

    def mark_order_created(self, order_id: str | None) -> None:
        if order_id:
            self.order_id = str(order_id)
            self.order_created_at = _now()

    def end_turn(self, since: float | None = None) -> None:
        """`since`: perf_counter() de cuando llegó el mensaje (incluye registro/Whisper). Si no, desde begin_turn."""
        if self._turn is None:
            return
        start = since if since is not None else self._t0
        self._turn["handle_ms"] = round((time.perf_counter() - start) * 1000, 1)
        self._turns.append(self._turn)
        self._turn = None

    # --- Envío -----------------------------------------------------------------

    def payload(self) -> dict | None:
        """El cuerpo para el backend, o None si en esta conversación aún no se creó un pedido."""
        if not (self.order_id and self.order_created_at and self.first_message_at and self._turns):
            return None
        return {
            "order_id": self.order_id,
            "first_message_at": self.first_message_at.isoformat(),
            "order_created_at": self.order_created_at.isoformat(),
            "turns": self._turns,
        }
