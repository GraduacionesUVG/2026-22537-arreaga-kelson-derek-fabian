"""Schemas de POST /api/internal/conversation-metrics (lo llama `agent/`)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TurnMetric(BaseModel):
    input_type: Literal["text", "audio", "other"] = "text"
    audio_ms: float = Field(default=0, ge=0)  # descarga + transcripción (solo audios)
    llm_ms: float = Field(default=0, ge=0)
    tools_ms: float = Field(default=0, ge=0)
    handle_ms: float = Field(ge=0)  # desde que llega el mensaje hasta tener la respuesta lista
    tools: list[str] = Field(default_factory=list, max_length=20)


class ConversationMetricIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    first_message_at: datetime
    order_created_at: datetime
    turns: list[TurnMetric] = Field(min_length=1, max_length=100)


class ConversationMetricOut(BaseModel):
    order_id: str
    duration_s: float
    bot_processing_s: float
    turns: int
