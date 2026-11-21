"""
/api/internal/conversation-metrics -- lo llama `agent/` al crear un pedido para
guardar cuánto tardó la conversación (ver src/models/conversation_metric.py).

Exige X-Internal-Api-Key y falla cerrado si el backend no tiene INTERNAL_API_KEY.
"""

from fastapi import APIRouter, Depends

from src.api.v1.deps import get_conversation_metric_service, require_internal_api_key_strict
from src.schemas.conversation_metric import ConversationMetricIn, ConversationMetricOut
from src.services.conversation_metric_service import ConversationMetricService

router = APIRouter(dependencies=[Depends(require_internal_api_key_strict)])


@router.post("/conversation-metrics", response_model=ConversationMetricOut)
async def record_conversation_metric(
    data: ConversationMetricIn, service: ConversationMetricService = Depends(get_conversation_metric_service)
):
    return await service.record(data)
