from datetime import datetime

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    environment: str
    project_name: str
    database_connected: bool


class CheckResult(BaseModel):
    """Resultado de una verificación. `status`: operational | degraded | down | unknown."""

    status: str
    detail: str | None = None
    latency_ms: int | None = None
    # Datos extra de la verificación (componentes afectados, calidad del número, etc.)
    data: dict | None = None


class IntegrationHealth(BaseModel):
    """Salud de un proveedor externo: `platform` es lo que reporta su página de
    estado (público); `credentials` es un sondeo con NUESTRAS llaves (`None` si
    se pidió sin sondeo)."""

    status: str
    platform: CheckResult
    credentials: CheckResult | None = None


class IntegrationsHealthResponse(BaseModel):
    checked_at: datetime
    status: str
    openai: IntegrationHealth
    whatsapp: IntegrationHealth
