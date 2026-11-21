import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from src.api.v1.routes.admin_control import router as admin_control_router
from src.api.v1.routes.admin_kds import router as admin_kds_router
from src.api.v1.routes.admin_menu_excel import router as admin_menu_excel_router
from src.api.v1.routes.admin_restaurant import router as admin_restaurant_router
from src.api.v1.routes.admin_shifts import router as admin_shifts_router
from src.api.v1.routes.auth import router as auth_router
from src.api.v1.routes.health import router as health_router
from src.api.v1.routes.kds import router as kds_router
from src.api.v1.routes.kds_stream import router as kds_stream_router
from src.api.v1.routes.menus import router as menus_router
from src.api.v1.routes.internal_metrics import router as internal_metrics_router
from src.api.v1.routes.metrics import router as metrics_router
from src.api.v1.routes.orders import router as orders_router
from src.api.v1.routes.restaurants import router as restaurants_router
from src.api.v1.routes.ti import router as ti_router
from src.api.v1.routes.users import router as users_router
from src.api.v1.routes.webhooks import router as webhooks_router
from src.configuration import settings
from src.core.cors import DEV_ORIGINS, parse_origins, setup_cors
from src.core.database import mongo
from src.core.security_headers import SecurityHeadersMiddleware
from src.integrations.events import event_publisher, relay_events_to_kds
from src.repositories.user_repo import ensure_user_phone_indexes
from src.services.payment_expiry import run_payment_expiry_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup - Conectar a MongoDB
    mongo.connect()
    # Número de WhatsApp cifrado (src/core/pii.py): quita el índice único viejo sobre el
    # número en claro y crea el del hash. Idempotente; si falla no frena el arranque.
    try:
        await ensure_user_phone_indexes(mongo.db)
    except Exception:
        logging.getLogger(__name__).exception("No se pudieron ajustar los índices de users.")

    # Publicador de eventos de pedidos (registro + WebSocket + Kafka opcional,
    # ver src/integrations/events.py).
    await event_publisher.start()
    relay_task = asyncio.create_task(relay_events_to_kds()) if settings.kafka.enabled else None
    # Cancela solicitudes aceptadas cuyo link de pago venció.
    expiry_task = asyncio.create_task(run_payment_expiry_loop())

    yield

    # Teardown
    expiry_task.cancel()
    if relay_task is not None:
        relay_task.cancel()
    await event_publisher.stop()
    mongo.close()


# Documentación interactiva (/docs, /redoc, /openapi.json): apagada en PRODUCTION
# salvo API_DOCS_ENABLED=true.
_docs_enabled = (
    settings.api_docs_enabled
    if settings.api_docs_enabled is not None
    else settings.environment.upper() != "PRODUCTION"
)

app = FastAPI(
    title=settings.project_name,
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# CORS para los frontends (kds, admin, ops, register), que viven en otros dominios.
# Sin CORS_ORIGINS queda cerrado, salvo en DEVELOPMENT (puertos locales).
_cors_origins = parse_origins(settings.cors.origins)
if not _cors_origins and settings.environment.upper() == "DEVELOPMENT":
    _cors_origins = DEV_ORIGINS
setup_cors(app, _cors_origins, settings.cors.origin_regex)
# Headers de seguridad (nosniff, sin iframes, CSP, HSTS en PRODUCTION). Va después de CORS
# para quedar por fuera: también se aplica a las respuestas que CORS genera (preflight).
app.add_middleware(SecurityHeadersMiddleware, hsts=settings.environment.upper() == "PRODUCTION")

# Registro de rutas — mismos prefijos
app.include_router(health_router, prefix="/health", tags=["System"])
app.include_router(auth_router, prefix="/auth", tags=["Auth"])
app.include_router(webhooks_router, prefix="/webhooks", tags=["Webhooks"])
app.include_router(orders_router, prefix="/api/orders", tags=["Orders"])
app.include_router(restaurants_router, prefix="/api/restaurants", tags=["Restaurants"])
app.include_router(menus_router, prefix="/api/menus", tags=["Menus"])
app.include_router(metrics_router, prefix="/api/metrics", tags=["Metrics / KPIs"])
app.include_router(kds_router, prefix="/api/kds", tags=["KDS"])
# Historial y memoria por estudiante para el agente de WhatsApp (X-Internal-Api-Key)
app.include_router(internal_metrics_router, prefix="/api/internal", tags=["Internal · Agente"])
app.include_router(users_router, prefix="/api/users", tags=["Users · Memoria del agente"])
# Panel de administración del restaurante (las rutas de Excel van antes que /menu/{item_id})
app.include_router(admin_menu_excel_router, prefix="/api/admin", tags=["Admin · Menú Excel"])
app.include_router(admin_restaurant_router, prefix="/api/admin", tags=["Admin · Restaurante y menú"])
app.include_router(admin_kds_router, prefix="/api/admin", tags=["Admin · KDS"])
app.include_router(admin_control_router, prefix="/api/admin", tags=["Admin · Control"])
app.include_router(admin_shifts_router, prefix="/api/admin", tags=["Admin · Turnos"])
# Panel Dev/TI
app.include_router(ti_router, prefix="/api/ti", tags=["Dev/TI"])
app.include_router(kds_stream_router, prefix="/ws", tags=["Realtime"])

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
