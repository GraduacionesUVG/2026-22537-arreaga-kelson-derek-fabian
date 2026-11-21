"""Schemas del panel de administración (restaurante, menú, KDS, control, Dev/TI)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, computed_field, model_validator

from src.core.timeutils import is_valid_hhmm, local_date_str
from src.models.restaurant import MAX_SERVICE_FEE

# --- Restaurante -----------------------------------------------------------------


class DayHoursIn(BaseModel):
    closed: bool = False
    open: str | None = Field(default=None, description="HH:MM (24 h)")
    close: str | None = Field(default=None, description="HH:MM (24 h)")

    @model_validator(mode="after")
    def _check(self):
        if self.closed:
            self.open, self.close = None, None
            return self
        if not is_valid_hhmm(self.open) or not is_valid_hhmm(self.close):
            raise ValueError("open y close deben tener formato HH:MM cuando el día no está cerrado.")
        if self.open >= self.close:
            raise ValueError("La hora de apertura debe ser menor que la de cierre.")
        return self


class OpeningHoursIn(BaseModel):
    monday: DayHoursIn
    tuesday: DayHoursIn
    wednesday: DayHoursIn
    thursday: DayHoursIn
    friday: DayHoursIn
    saturday: DayHoursIn
    sunday: DayHoursIn


class DayHoursOut(BaseModel):
    closed: bool
    open: str | None = None
    close: str | None = None


class LocationIn(BaseModel):
    location: str = Field(min_length=1, max_length=300)


class ServiceFeeIn(BaseModel):
    service_fee: float = Field(
        ge=0,
        le=MAX_SERVICE_FEE,
        description="Tarifa fija (Q) que se le suma al cliente en cada pedido. 0 = sin tarifa.",
    )

    @model_validator(mode="after")
    def _round(self):
        self.service_fee = round(self.service_fee, 2)
        return self


class RestaurantAdminOut(BaseModel):
    id: str
    name: str
    location: str | None = None
    active: bool = True
    service_fee: float = Field(default=0.0, description="Tarifa de servicio por pedido (Q).")
    opening_hours: dict[str, DayHoursOut]
    is_open_now: bool
    payment_gateway_configured: bool = False


class PaymentGatewayIn(BaseModel):
    """Credenciales de la cuenta de Tilopay del restaurante (admin.tilopay.com → Conexión a plataformas)."""

    api_user: str = Field(min_length=1, max_length=200)
    api_password: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=4, max_length=200)
    currency: Literal["GTQ", "USD"] = "GTQ"


class PaymentGatewayOut(BaseModel):
    """Estado de la cuenta de pagos. Nunca incluye la contraseña ni la API key completa."""

    provider: str = "tilopay"
    configured: bool
    api_user: str | None = None
    api_key_last4: str | None = None
    currency: str | None = None
    verified_at: datetime | None = None
    updated_at: datetime | None = None


# --- Menú --------------------------------------------------------------------------

ScheduleTypeLiteral = Literal["always", "weekly", "date"]
WeekdayLiteral = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class MenuModifierIn(BaseModel):
    id: str | None = None
    group: str | None = None
    name: str = Field(min_length=1)
    price: float = Field(default=0.0, ge=0)


class MenuModifierOut(BaseModel):
    id: str
    group: str | None = None
    name: str
    price: float


class MenuItemIn(BaseModel):
    code: str | None = Field(default=None, description="Si no se envía, se genera.")
    name: str
    category: str
    description: str | None = None
    price: float = Field(ge=0)
    available: bool = True
    schedule_type: ScheduleTypeLiteral = "always"
    day_of_week: WeekdayLiteral | None = Field(default=None, description="Obligatorio si schedule_type = weekly")
    serve_date: str | None = Field(default=None, description="YYYY-MM-DD, obligatorio si schedule_type = date")
    option_number: int | None = None
    meal_period: Literal["desayuno", "almuerzo", "cena"] | None = None
    prep_time_minutes: float = Field(default=5.0, ge=0)
    ready_from: str | None = Field(default=None, description="HH:MM. Aviso 'listo aprox. desde'.")
    order_cutoff: str | None = Field(default=None, description="HH:MM. Hora límite para pedirlo ese día.")
    daily_stock: int | None = Field(default=None, ge=1)
    dine_in_only: bool = False
    modifiers: list[MenuModifierIn] = Field(default_factory=list)


class MenuItemUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    category: str | None = None
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    available: bool | None = None
    schedule_type: ScheduleTypeLiteral | None = None
    day_of_week: WeekdayLiteral | None = None
    serve_date: str | None = None
    option_number: int | None = None
    meal_period: Literal["desayuno", "almuerzo", "cena"] | None = None
    prep_time_minutes: float | None = Field(default=None, ge=0)
    ready_from: str | None = None
    order_cutoff: str | None = None
    daily_stock: int | None = Field(default=None, ge=1)
    dine_in_only: bool | None = None
    modifiers: list[MenuModifierIn] | None = None


class MenuItemOut(BaseModel):
    id: str
    restaurant_id: str
    code: str | None = None
    name: str
    category: str
    category_icon: str | None = Field(default=None, description="Clave del icono de la categoría.")
    description: str | None = None
    price: float
    available: bool = Field(default=True, description="false = desactivado permanente (hasta activarlo a mano).")
    paused_on: str | None = Field(default=None, description="Fecha (YYYY-MM-DD) en la que está desactivado solo por ese día.")
    schedule_type: str = "always"
    day_of_week: str | None = None
    serve_date: str | None = None
    option_number: int | None = None
    meal_period: str | None = None
    prep_time_minutes: float = 5.0
    ready_from: str | None = None
    order_cutoff: str | None = None
    daily_stock: int | None = None
    dine_in_only: bool = False
    modifiers: list[MenuModifierOut] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @computed_field
    @property
    def inactive_mode(self) -> str | None:
        """`permanent`, `daily` (vuelve mañana) o `null` si está activo."""
        if not self.available:
            return "permanent"
        if self.paused_on and self.paused_on == local_date_str():
            return "daily"
        return None

    @computed_field
    @property
    def active(self) -> bool:
        """Disponibilidad efectiva en este momento (lo que ve el chatbot)."""
        return self.inactive_mode is None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    icon: str | None = Field(
        default=None,
        description="Clave del icono (ver GET /menu/category-icons). Si no se envía se sugiere según el nombre.",
    )


class CategoryUpdate(BaseModel):
    """Body de PATCH /menu/categories/{name}: al menos uno de los dos."""

    name: str | None = Field(default=None, min_length=1, max_length=60, description="Nombre nuevo (renombra en cascada).")
    icon: str | None = Field(default=None, description="Clave del icono.")


class CategoryOut(BaseModel):
    name: str
    icon: str


class CategoryDetailOut(BaseModel):
    name: str
    icon: str
    products_count: int = 0


class CategoryIconsOut(BaseModel):
    icons: list[str]
    default: str


class MenuAvailabilityIn(BaseModel):
    """Body de PATCH /api/kds/menu/{item_id}/availability."""

    active: bool
    mode: Literal["permanent", "daily"] | None = Field(
        default=None,
        description="Obligatorio si active = false. permanent: hasta activarlo a mano. daily: se activa solo al día siguiente.",
    )

    @model_validator(mode="after")
    def _check(self):
        if not self.active and self.mode is None:
            raise ValueError("Al desactivar se debe indicar mode: permanent o daily.")
        return self


class ImportRowError(BaseModel):
    sheet: str
    row: int | None = None
    column: str | None = None
    error: str


class ImportResult(BaseModel):
    dry_run: bool
    created: int = 0
    updated: int = 0
    replaced_dates: list[str] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    items: list[MenuItemOut] = Field(default_factory=list)


# --- KDS -----------------------------------------------------------------------------


class MenuFilterIn(BaseModel):
    mode: Literal["all", "custom"] = "all"
    item_ids: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class KdsUserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    pin: str = Field(pattern=r"^\d{4,8}$")
    priority: int = Field(default=1, ge=1)
    menu_filter: MenuFilterIn = Field(default_factory=MenuFilterIn)
    active: bool = True


class KdsUsernameIn(BaseModel):
    username: str = Field(min_length=3, max_length=40)


class KdsPinIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4,8}$", description="Solo números, entre 4 y 8 dígitos.")


class KdsPriorityIn(BaseModel):
    priority: int = Field(ge=1, description="1 = más alta")


class KdsActiveIn(BaseModel):
    active: bool


class KdsStatusOut(BaseModel):
    kds_user_id: str
    username: str
    restaurant_id: str | None = None
    restaurant_name: str | None = None
    connected: bool
    connected_since: datetime | None = None
    last_seen: datetime | None = None


class KdsUserOut(KdsStatusOut):
    priority: int = 1
    active: bool = True
    menu_filter: MenuFilterIn
    last_login_at: datetime | None = None


# --- Control -------------------------------------------------------------------------------


class OrderEventOut(BaseModel):
    id: str
    seq: int
    event: str
    restaurant_id: str | None = None
    kds_user_ids: list[str] = Field(default_factory=list)
    order_id: str | None = None
    request_number: int | None = None
    order_number: int | None = None
    pipeline_status: str | None = None
    payment_status: str | None = None
    actor: dict | None = None
    message: str
    data: dict = Field(default_factory=dict)
    created_at: datetime
    local_date: str


class EventsDayOut(BaseModel):
    date: str
    live: bool = Field(description="true = hoy y el restaurante sigue abierto: conviene conectarse al WebSocket.")
    events: list[OrderEventOut]


# --- Dev/TI ------------------------------------------------------------------------------------


class RestaurantCreateIn(BaseModel):
    restaurant_name: str = Field(min_length=2, max_length=80)
    admin_username: str = Field(min_length=2, max_length=60)
    admin_email: EmailStr
    location: str | None = Field(default=None, max_length=300)
    service_fee: float = Field(default=0.0, ge=0, le=MAX_SERVICE_FEE, description="Tarifa de servicio por pedido (Q).")


class AdminUserCreateIn(BaseModel):
    restaurant_id: str
    username: str = Field(min_length=2, max_length=60)
    email: EmailStr
    password: str | None = Field(
        default=None,
        min_length=8,
        description="Opcional. Si no se envía, se manda por correo el enlace para crear la contraseña.",
    )


class AdminUserOut(BaseModel):
    id: str
    email: str
    username: str | None = None
    role: str
    restaurant_id: str | None = None
    restaurant_name: str | None = None
    must_change_password: bool
    password_pending: bool = Field(description="true = todavía no ha definido su contraseña")
    last_login_at: datetime | None = None
    created_at: datetime | None = None
