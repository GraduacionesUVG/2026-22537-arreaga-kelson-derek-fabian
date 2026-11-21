"""
Configuración centralizada del proyecto (patrón de la plantilla original,
adaptado con secciones nuevas para cada integración externa del protocolo:
MongoDB, WhatsApp, Tilopay y Microsoft Entra ID).

Todos los valores se leen de variables de entorno (ver .env.example en la
raíz del proyecto). En Docker, docker-compose.yml inyecta estas variables
vía `env_file: .env` + overrides puntuales (ej. MONGO_URI apuntando al
servicio `mongo`).
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # raíz del proyecto (donde vive .env)


class MongoSettings(BaseSettings):
    """Conexión a MongoDB.

    En Docker, `uri` apunta al servicio `mongo` de docker-compose.yml
    (mongodb://mongo:27017). En local sin Docker, usa localhost.
    """

    uri: str = Field("mongodb://localhost:27017", alias="MONGO_URI")
    db_name: str = Field("gp_agent_db", alias="MONGO_DB_NAME")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class WhatsAppSettings(BaseSettings):
    """Credenciales del canal WhatsApp (Meta Cloud API).

    TODO: completar cuando se tenga acceso a la app de Meta for Developers.
    """

    token: str = Field("", alias="WHATSAPP_TOKEN")
    phone_number_id: str = Field("", alias="WHATSAPP_PHONE_NUMBER_ID")
    verify_token: str = Field("", alias="WHATSAPP_VERIFY_TOKEN")

    # Message Templates (botones con URL en vez de pegar el link completo).
    # Apagado por defecto: hasta que Meta apruebe las plantillas, los avisos
    # siguen saliendo como texto.
    templates_enabled: bool = Field(False, alias="WHATSAPP_TEMPLATES_ENABLED")
    template_language: str = Field("es", alias="WHATSAPP_TEMPLATE_LANGUAGE")
    template_payment: str = Field("link_de_pago", alias="WHATSAPP_TEMPLATE_PAYMENT")
    template_delivered: str = Field("pedido_entregado", alias="WHATSAPP_TEMPLATE_DELIVERED")
    # Base de la URL del botón dinámico de pago, tal como se registró en Meta
    # (la parte antes de {{1}}). Sin valor hasta que exista la página de pago.
    payment_url_base: str = Field("", alias="PAYMENT_URL_BASE")
    # Página (estática) de calificación: va en el botón del template de
    # entregado y en el texto de respaldo.
    rating_url: str = Field("", alias="RATING_URL")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class CorsSettings(BaseSettings):
    """Orígenes (frontends) autorizados a llamar al API desde el navegador."""

    # URLs separadas por coma, sin ruta: https://qorder-admin.onrender.com,https://qorder-kds.onrender.com
    origins: str = Field("", alias="CORS_ORIGINS")
    # Opcional: regex para orígenes variables (previews de Render).
    origin_regex: str = Field("", alias="CORS_ORIGIN_REGEX")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class OpenAISettings(BaseSettings):
    """Llave y modelo del orquestador (los usa agent/); el backend solo los lee
    para el health de integraciones (GET /health/integrations)."""

    api_key: str = Field("", alias="OPENAI_API_KEY")
    model: str = Field("gpt-4o-mini", alias="ORCHESTRATOR_MODEL")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class TilopaySettings(BaseSettings):
    """Ajustes generales de Tilopay (links de pago, ver src/integrations/tilopay_client.py).

    Las credenciales (API user, password y key) NO van aquí: cada restaurante
    tiene su propia cuenta de Tilopay y las carga desde el panel de
    administración (`PUT /api/admin/restaurant/payment-gateway`). Se guardan
    cifradas en `restaurants.payment_gateway` con `PAYMENT_CREDENTIALS_SECRET`
    (ver src/services/payment_accounts.py). Tilopay usa el mismo host para
    pruebas y producción: el modo se cambia en el portal de cada cuenta.
    """

    base_url: str = Field("https://app.tilopay.com", alias="TILOPAY_BASE_URL")
    # Moneda por defecto al configurar la cuenta de un restaurante.
    default_currency: str = Field("GTQ", alias="TILOPAY_DEFAULT_CURRENCY")
    # Secreto para firmar la URL del webhook de cada pedido; vacío = usa JWT_SECRET_KEY.
    webhook_secret: str = Field("", alias="TILOPAY_WEBHOOK_SECRET")
    timeout_seconds: float = Field(15.0, alias="TILOPAY_TIMEOUT_SECONDS")
    # Clave para cifrar las credenciales de Tilopay de cada restaurante en Mongo (cualquier texto
    # largo y aleatorio). Obligatoria en PRODUCTION; en desarrollo, vacía = deriva de JWT_SECRET_KEY.
    # Si se cambia, las credenciales guardadas ya no se pueden leer y cada restaurante debe volver a cargarlas.
    credentials_secret: str = Field("", alias="PAYMENT_CREDENTIALS_SECRET")
    # Pagos simulados (POST /webhooks/bac-payment) para restaurantes SIN cuenta de Tilopay.
    # Vacío = permitidos fuera de PRODUCTION y prohibidos en PRODUCTION.
    simulated_payments_enabled: bool | None = Field(None, alias="SIMULATED_PAYMENTS_ENABLED")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class EntraIdSettings(BaseSettings):
    """Credenciales de Microsoft Entra ID (SSO institucional).

    TODO: completar con los datos del tenant de la UVG.
    """

    client_id: str = Field("", alias="ENTRA_CLIENT_ID")
    tenant_id: str = Field("", alias="ENTRA_TENANT_ID")
    client_secret: str = Field("", alias="ENTRA_CLIENT_SECRET")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class AuthSettings(BaseSettings):
    """Parámetros del sistema de autenticación (tres flujos independientes):

    - WhatsApp (estudiantes): verificación de correo institucional por código OTP.
    - Administración (`admin_users`): correo + contraseña -> JWT.
    - KDS (`kds_users`): usuario + PIN numérico -> JWT.
    """

    jwt_algorithm: str = Field("HS256", alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(480, alias="JWT_EXPIRE_MINUTES")  # 8h, un turno de KDS

    otp_code_expire_minutes: int = Field(10, alias="OTP_CODE_EXPIRE_MINUTES")
    max_verification_requests: int = Field(3, alias="MAX_VERIFICATION_REQUESTS")
    verification_request_window_hours: int = Field(24, alias="VERIFICATION_REQUEST_WINDOW_HOURS")
    max_otp_attempts: int = Field(5, alias="MAX_OTP_ATTEMPTS")

    inactive_user_ttl_days: int = Field(180, alias="INACTIVE_USER_TTL_DAYS")  # ~6 meses

    backend_base_url: str = Field("http://localhost:8000", alias="BACKEND_BASE_URL")

    # URL de la página de login/registro (frontend) que el bot le manda al
    # estudiante cuando su número todavía no está registrado o verificado
    # (ver AuthService.check_registration).
    prod_login_url: str = Field("https://qorder.lat/login", alias="PROD_LOGIN_URL")

    # Header compartido opcional para proteger POST /auth/whatsapp/check (lo
    # llama el servicio agent/, no un usuario final). Si queda vacío (default),
    # el endpoint no exige el header -- útil en desarrollo/pruebas locales.
    internal_api_key: str = Field("", alias="INTERNAL_API_KEY")

    # Panel de administración (frontend): base de los enlaces de cambio de
    # contraseña enviados por correo (`<admin_frontend_url>/reset-password?token=...`).
    admin_frontend_url: str = Field("http://localhost:5173", alias="ADMIN_FRONTEND_URL")
    password_reset_expire_minutes: int = Field(60, alias="PASSWORD_RESET_EXPIRE_MINUTES")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class EmailSettings(BaseSettings):
    """Credenciales de Resend (API HTTP, no SMTP) para el envío de correos
    de verificación.

    Se usa la API HTTP de Resend (https://api.resend.com/emails) en vez de
    SMTP porque los planes free de Render bloquean el trafico saliente a
    los puertos SMTP (25/465/587) desde el 26 de septiembre de 2025 -- ver
    https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports.
    La API HTTP usa el puerto 443 (HTTPS normal), que nunca se bloquea.

    Si `resend_api_key` queda vacío (default), src/integrations/email_client.py
    no falla: imprime el correo en consola, útil para desarrollo/pruebas
    sin credenciales reales.
    """

    resend_api_key: str = Field("", alias="RESEND_API_KEY")
    from_address: str = Field("no-reply@gp-agent.local", alias="EMAIL_FROM_ADDRESS")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class KafkaSettings(BaseSettings):
    """Conexión a Kafka para la publicación/consumo de eventos de pedidos.

    Deshabilitado por defecto (`enabled=False`): el push al KDS por
    WebSocket funciona igual sin esto en un despliegue de una sola
    instancia del API -- Kafka solo hace falta para replicar eventos entre
    varias instancias y para que src/worker.py mande los WhatsApp de forma
    desacoplada. `bootstrap_servers` apunta por default al servicio `kafka`
    de docker-compose.yml (perfil `kafka`, ver docker-compose.yml).
    """

    enabled: bool = Field(False, alias="KAFKA_ENABLED")
    bootstrap_servers: str = Field("kafka:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    # Sin Kafka (o si un evento no se pudo publicar), el API manda él mismo
    # el WhatsApp al estudiante en una tarea de fondo -- ver
    # src/integrations/events.py (punto 3) y src/services/customer_notifications.py.
    inline_notifications: bool = Field(True, alias="NOTIFY_INLINE_WITHOUT_KAFKA")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class PaymentSettings(BaseSettings):
    """Ventana para pagar un pedido aceptado (ver OrderService.compute_payment_deadline).

    - Pedido "lo antes posible": `asap_minutes`.
    - Pedido con hora solicitada: hora solicitada - tiempo de preparación - 5 min,
      acotado entre `min_minutes` y `max_minutes`.
    """

    asap_minutes: int = Field(10, alias="PAYMENT_LINK_ASAP_MINUTES")
    min_minutes: int = Field(5, alias="PAYMENT_LINK_MIN_MINUTES")
    max_minutes: int = Field(15, alias="PAYMENT_LINK_MAX_MINUTES")
    expiry_check_seconds: int = Field(30, alias="PAYMENT_EXPIRY_CHECK_SECONDS")
    # Minutos que una solicitud puede esperar a que el restaurante la acepte o rechace antes de
    # rechazarse sola ("el restaurante no respondió"). 0 = nunca expira.
    request_timeout_minutes: int = Field(20, alias="REQUEST_ACCEPTANCE_TIMEOUT_MINUTES")
    # Último pedido del día: no se aceptan pedidos en los últimos N minutos antes del cierre,
    # para que la cocina no se quede esperando al cliente pasada su hora de cierre.
    last_order_minutes_before_close: int = Field(30, alias="LAST_ORDER_MINUTES_BEFORE_CLOSE")
    # Monto mínimo de un pedido (suma de productos, sin la tarifa de servicio). Pedidos por
    # debajo se rechazan en /api/orders/estimate y /api/orders/ con 400 `below_minimum_order`.
    # 0 = sin mínimo.
    min_order_amount: float = Field(25.0, alias="MIN_ORDER_AMOUNT")

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


class Settings(BaseSettings):
    environment: str = Field("DEVELOPMENT", alias="ENVIRONMENT")
    project_name: str = Field("GP-Agent-Backend", alias="PROJECT_NAME")
    jwt_secret_key: str = Field("change-me", alias="JWT_SECRET_KEY")
    # Clave para cifrar datos personales de estudiantes en Mongo (hoy el número de WhatsApp,
    # ver src/core/pii.py). Obligatoria en PRODUCTION; en desarrollo, vacía = deriva de
    # JWT_SECRET_KEY. Si se cambia o se pierde, los números guardados ya no se pueden leer.
    user_data_secret: str = Field("", alias="USER_DATA_SECRET")
    # /docs, /redoc y /openapi.json. Sin definir: activos fuera de PRODUCTION, apagados en PRODUCTION.
    api_docs_enabled: bool | None = Field(None, alias="API_DOCS_ENABLED")

    mongo: MongoSettings = Field(default_factory=MongoSettings)
    whatsapp: WhatsAppSettings = Field(default_factory=WhatsAppSettings)
    tilopay: TilopaySettings = Field(default_factory=TilopaySettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    entra_id: EntraIdSettings = Field(default_factory=EntraIdSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    payment: PaymentSettings = Field(default_factory=PaymentSettings)

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore")


settings = Settings()
