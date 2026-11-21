"""
/api/users/{student_id}/... -- historial y memoria de un estudiante para el
agente de WhatsApp (agent/memory.py).

Todas las rutas exigen X-Internal-Api-Key y fallan cerrado si el backend no
tiene INTERNAL_API_KEY: el student_id (carné) se puede adivinar, así que sin
esto cualquiera podría leer lo que pidió y dijo otro estudiante.
"""

from fastapi import APIRouter, Depends, Response, status

from src.api.v1.deps import get_user_memory_service, require_internal_api_key_strict
from src.schemas.user_memory import OrderHistoryOut, UserMemoryOut, UserMemoryUpdate
from src.services.user_memory_service import UserMemoryService

router = APIRouter(dependencies=[Depends(require_internal_api_key_strict)])


@router.get("/{student_id}/order-history", response_model=OrderHistoryOut)
async def get_order_history(student_id: str, service: UserMemoryService = Depends(get_user_memory_service)):
    return await service.get_order_history(student_id)


@router.get("/{student_id}/memory", response_model=UserMemoryOut)
async def get_memory(student_id: str, service: UserMemoryService = Depends(get_user_memory_service)):
    return await service.get_memory(student_id)


@router.put("/{student_id}/memory", response_model=UserMemoryOut)
async def update_memory(
    student_id: str,
    data: UserMemoryUpdate,
    service: UserMemoryService = Depends(get_user_memory_service),
):
    """Reemplaza solo las secciones enviadas (preferences / summaries / session)."""
    return await service.update_memory(student_id, data)


@router.delete("/{student_id}/memory", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(student_id: str, service: UserMemoryService = Depends(get_user_memory_service)):
    """"Olvida mis datos": borra la memoria (no los pedidos)."""
    await service.delete_memory(student_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
