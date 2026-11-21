"""
Tiempos de conversación del agente (objetivo: pedido estándar < 45 s).

Dos medidas por pedido (ver src/models/conversation_metric.py):
- `duration_s`: primer mensaje del estudiante -> pedido creado (incluye el tiempo del usuario).
- `bot_processing_s`: suma de `handle_ms` de los turnos (lo que tardó el sistema).
"""

from statistics import mean

from src.core.timeutils import as_utc, local_date_str
from src.models.conversation_metric import TARGET_SECONDS
from src.repositories.conversation_metric_repo import ConversationMetricRepository
from src.schemas.conversation_metric import ConversationMetricIn
from src.services.kpi_service import _percentile


def _stats(values: list[float]) -> dict:
    if not values:
        return {"muestras": 0, "promedio": None, "mediana": None, "p90": None, "p95": None, "maximo": None, "pct_bajo_45s": None}
    under = sum(1 for v in values if v < TARGET_SECONDS)
    return {
        "muestras": len(values),
        "promedio": round(mean(values), 1),
        "mediana": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "p95": _percentile(values, 0.95),
        "maximo": round(max(values), 1),
        "pct_bajo_45s": round(under / len(values), 4),
    }


class ConversationMetricService:
    def __init__(self, repo: ConversationMetricRepository):
        self.repo = repo

    async def record(self, data: ConversationMetricIn) -> dict:
        first, created = as_utc(data.first_message_at), as_utc(data.order_created_at)
        duration_s = max(0.0, (created - first).total_seconds())
        bot_s = sum(t.handle_ms for t in data.turns) / 1000
        doc = {
            "order_id": data.order_id,
            "first_message_at": first,
            "order_created_at": created,
            "duration_s": round(duration_s, 2),
            "bot_processing_s": round(bot_s, 2),
            "turns": len(data.turns),
            "input_types": sorted({t.input_type for t in data.turns}),
            "turn_log": [t.model_dump() for t in data.turns],
            "local_date": local_date_str(created),
        }
        await self.repo.upsert_by_order(data.order_id, doc)
        return {"order_id": data.order_id, "duration_s": doc["duration_s"], "bot_processing_s": doc["bot_processing_s"], "turns": doc["turns"]}

    async def summary(self, from_date: str | None, to_date: str | None) -> dict:
        docs = await self.repo.in_range(from_date, to_date)
        audio_ms = [t["audio_ms"] for d in docs for t in d.get("turn_log", []) if t.get("input_type") == "audio"]
        llm_ms = [t["llm_ms"] for d in docs for t in d.get("turn_log", [])]
        return {
            "objetivo_segundos": TARGET_SECONDS,
            "pedidos": len(docs),
            "tiempo_total_s": _stats([d["duration_s"] for d in docs]),
            "procesamiento_bot_s": _stats([d["bot_processing_s"] for d in docs]),
            "turnos_por_pedido": round(mean(d["turns"] for d in docs), 2) if docs else None,
            "mediana_audio_ms": _percentile(audio_ms, 0.5),
            "mediana_llm_ms_por_turno": _percentile(llm_ms, 0.5),
        }
