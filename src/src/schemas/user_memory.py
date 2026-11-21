"""Schemas de /api/users/{student_id}/... (memoria e historial para el agente de WhatsApp)."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from src.models.user_memory import (
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_SUMMARIES,
    MAX_SUMMARY_CHARS,
    MAX_TURNS,
)

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ITEM_CHARS)]
ShortList = Annotated[list[ShortText], Field(default_factory=list, max_length=MAX_LIST_ITEMS)]


class Preferences(BaseModel):
    # Para agregar una preferencia nueva (ej. favorite_restaurants): un campo
    # más con default vacío -- los documentos viejos se leen igual.
    likes: ShortList
    dislikes: ShortList
    restrictions: ShortList
    usual_budget: float | None = Field(default=None, ge=0, le=10_000)


class SessionSummary(BaseModel):
    date: datetime
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SUMMARY_CHARS)]


class SessionState(BaseModel):
    started_at: datetime | None = None
    last_message_at: datetime | None = None
    # Cada turno es la lista de mensajes de OpenAI (user, assistant, tool...)
    # tal cual; el agente corta por turnos completos.
    turns: list[list[dict[str, Any]]] = Field(default_factory=list, max_length=MAX_TURNS)


class UserMemoryUpdate(BaseModel):
    """PUT: solo se reemplazan las secciones que vienen; las demás no se tocan."""

    preferences: Preferences | None = None
    summaries: list[SessionSummary] | None = Field(default=None, max_length=MAX_SUMMARIES)
    session: SessionState | None = None


class UserMemoryOut(BaseModel):
    student_id: str
    schema_version: int
    preferences: Preferences
    summaries: list[SessionSummary]
    session: SessionState
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProductCount(BaseModel):
    product: str
    count: int


class RestaurantCount(BaseModel):
    restaurant: str
    count: int


class RecentOrder(BaseModel):
    date: datetime
    restaurant: str
    items: list[str]
    total: float


class OrderHistoryOut(BaseModel):
    """Resumen calculado de los pedidos pagados (no rechazados/cancelados) del estudiante."""

    total_orders: int
    most_ordered: list[ProductCount]
    frequent_restaurants: list[RestaurantCount]
    recent_orders: list[RecentOrder]
    tried_products: list[str]
    usual_hour: str | None = None  # "13:00", hora de Guatemala
    average_spend: float | None = None
