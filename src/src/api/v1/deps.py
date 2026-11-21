"""
Inyectores de dependencias compartidos por las rutas de la v1 de la API:
servicios (repository -> service) y autenticación/alcance (JWT).
"""

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.configuration import settings
from src.core.database import get_db
from src.core.security import decode_access_token
from src.models.admin_user import (
    ADMIN_ROLES,
    LEGACY_ADMIN_ROLE,
    AdminRole,
    normalize_admin_role,
)
from src.repositories.admin_user_repo import AdminUserRepository
from src.repositories.conversation_metric_repo import ConversationMetricRepository
from src.repositories.counter_repo import CounterRepository
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuCategoryRepository, MenuRepository
from src.repositories.order_repo import OrderRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.repositories.user_memory_repo import UserMemoryRepository
from src.repositories.user_repo import UserRepository
from src.services.auth_service import AuthService
from src.services.conversation_metric_service import ConversationMetricService
from src.services.menu_service import MenuService
from src.services.order_service import OrderService
from src.services.payment_service import PaymentService
from src.services.restaurant_service import RestaurantService
from src.services.user_memory_service import UserMemoryService
from src.services.user_service import UserService

# --- Servicios ----------------------------------------------------------------


def get_order_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> OrderService:
    return OrderService(
        OrderRepository(db),
        MenuRepository(db),
        RestaurantRepository(db),
        PaymentService(RestaurantRepository(db)),
        kds_repo=KdsUserRepository(db),
        counter_repo=CounterRepository(db),
        category_repo=MenuCategoryRepository(db),
    )


def get_menu_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> MenuService:
    return MenuService(
        MenuRepository(db), OrderRepository(db), MenuCategoryRepository(db), kds_repo=KdsUserRepository(db)
    )


def get_restaurant_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> RestaurantService:
    return RestaurantService(RestaurantRepository(db), KdsUserRepository(db))


def get_user_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> UserService:
    return UserService(UserRepository(db))


def get_user_memory_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> UserMemoryService:
    return UserMemoryService(UserMemoryRepository(db), OrderRepository(db), RestaurantRepository(db))


def get_conversation_metric_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> ConversationMetricService:
    return ConversationMetricService(ConversationMetricRepository(db))


def get_auth_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> AuthService:
    return AuthService(
        user_repo=UserRepository(db),
        admin_repo=AdminUserRepository(db),
        kds_repo=KdsUserRepository(db),
        db=db,
    )


# --- Autenticación (JWT) ------------------------------------------------------

_bearer_scheme = HTTPBearer(
    auto_error=True,
    scheme_name="Admin/KDS JWT",
    bearerFormat="JWT",
    description="Pega aquí el `access_token` de POST /auth/admin/login o POST /auth/kds/login.",
)


def get_current_admin_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Decodifica el JWT del header `Authorization: Bearer <token>` (cualquier rol)."""
    try:
        return decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def require_role(*roles: str):
    """Restringe una ruta a ciertos roles del token. `admin` incluye los dos roles de administración."""

    expanded = set(roles)
    if LEGACY_ADMIN_ROLE in expanded:
        expanded.update(ADMIN_ROLES)

    def _dependency(current_admin: dict = Depends(get_current_admin_user)) -> dict:
        role = current_admin.get("role")
        if role == LEGACY_ADMIN_ROLE:
            role = normalize_admin_role(role, current_admin.get("restaurant_id"))
        if role not in expanded and current_admin.get("role") not in expanded:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes permiso para esta acción.")
        return current_admin

    return _dependency


@dataclass
class AdminScope:
    admin_id: str
    role: str
    restaurant_id: str | None
    email: str | None = None
    username: str | None = None

    @property
    def is_superadmin(self) -> bool:
        return self.role == AdminRole.SUPERADMIN.value

    def resolve_restaurant(self, requested: str | None, allow_all: bool = False) -> str | None:
        """Restaurante sobre el que actúa la request.

        - `restaurant_admin`: siempre el suyo; pedir otro es 403.
        - `superadmin`: el solicitado; `all`/vacío devuelve `None` si `allow_all`, si no 400.
        """
        if not self.is_superadmin:
            if requested not in (None, "", "all", self.restaurant_id):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo puedes ver los datos de tu restaurante.")
            if not self.restaurant_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Tu usuario no tiene un restaurante asignado.")
            return self.restaurant_id
        if requested in (None, "", "all"):
            if allow_all:
                return None
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Indica el parámetro restaurant_id.")
        return requested


async def get_admin_scope(
    claims: dict = Depends(get_current_admin_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> AdminScope:
    if claims.get("role") not in (*ADMIN_ROLES, LEGACY_ADMIN_ROLE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Se requiere un usuario de administración.")
    admin = await AdminUserRepository(db).find_by_id(claims["sub"])
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "El usuario ya no existe.")
    return AdminScope(
        admin_id=admin["id"],
        role=normalize_admin_role(admin.get("role"), admin.get("restaurant_id")),
        restaurant_id=admin.get("restaurant_id"),
        email=admin.get("email"),
        username=admin.get("username"),
    )


async def require_superadmin(scope: AdminScope = Depends(get_admin_scope)) -> AdminScope:
    if not scope.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el equipo Dev/TI puede hacer esta acción.")
    return scope


async def load_kds_from_claims(claims: dict, db: AsyncIOMotorDatabase) -> dict:
    """Valida un token de KDS contra la base (existencia y versión de token)."""
    if claims.get("role") != "kds":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Se requiere un usuario de KDS.")
    kds_user = await KdsUserRepository(db).find_by_id(claims["sub"])
    if kds_user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "El usuario de KDS ya no existe.")
    if int(claims.get("tv", 0)) != int(kds_user.get("token_version", 0)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "La sesión del KDS ya no es válida, inicia sesión de nuevo.")
    return kds_user


async def get_current_kds(
    claims: dict = Depends(get_current_admin_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    return await load_kds_from_claims(claims, db)


async def get_order_actor(
    claims: dict = Depends(get_current_admin_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    """Quién opera un pedido: estación KDS o administrador."""
    if claims.get("role") == "kds":
        kds_user = await load_kds_from_claims(claims, db)
        return {
            "type": "kds",
            "id": kds_user["id"],
            "username": kds_user["username"],
            "restaurant_id": kds_user.get("restaurant_id"),
        }
    scope = await get_admin_scope(claims, db)
    return {
        "type": "admin",
        "id": scope.admin_id,
        "username": scope.username or scope.email,
        "restaurant_id": scope.restaurant_id,
        "is_superadmin": scope.is_superadmin,
    }


# --- Autenticación de servicio a servicio (agent/ -> backend) ---------------


def require_internal_api_key_strict(x_internal_api_key: str | None = Header(default=None)) -> None:
    """Rutas que solo llama `agent/` (y otros servicios internos). Falla CERRADO:
    sin INTERNAL_API_KEY configurado no deja pasar a nadie (503)."""
    expected = settings.auth.internal_api_key
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "INTERNAL_API_KEY no está configurado en el backend.")
    if not x_internal_api_key or not hmac.compare_digest(x_internal_api_key, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta o es inválido el header X-Internal-Api-Key.")
