from fastapi import APIRouter, Depends, Query

from src.api.v1.deps import AdminScope, require_superadmin
from src.configuration import settings
from src.core.database import mongo
from src.schemas.health import HealthResponse, IntegrationsHealthResponse
from src.services import integration_health

router = APIRouter()


@router.get("/", response_model=HealthResponse, tags=["System"])
def health_check():
    """Verifica que la API y la conexión a MongoDB estén vivas."""
    return HealthResponse(
        status="OK",
        environment=settings.environment,
        project_name=settings.project_name,
        database_connected=mongo.is_connected,
    )


@router.get("/integrations", response_model=IntegrationsHealthResponse, tags=["System"])
async def integrations_health(
    probe: bool = Query(True, description="false = solo las páginas de estado públicas, sin usar nuestras llaves."),
    refresh: bool = Query(False, description="true = ignora el caché de 60 s."),
    _: AdminScope = Depends(require_superadmin),
):
    """Salud de OpenAI y de la WhatsApp Cloud API (estado de la plataforma + validez
    de nuestras llaves). Solo Dev/TI: expone la calidad del número y el estado de los tokens."""
    return await integration_health.check_integrations(probe=probe, force=refresh)
