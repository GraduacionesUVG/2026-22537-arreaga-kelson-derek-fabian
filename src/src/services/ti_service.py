"""Panel Dev/TI: restaurantes, usuarios de administración y restablecimiento de contraseñas."""

import logging

from src.core.timeutils import utc_now
from src.integrations.email_client import EmailSendError
from src.models.admin_user import AdminRole, normalize_admin_role
from src.models.restaurant import DEFAULT_OPENING_HOURS, normalize_opening_hours
from src.repositories.admin_user_repo import AdminUserRepository
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.services.auth_service import AuthService
from src.services.payment_accounts import is_configured as payment_gateway_configured

logger = logging.getLogger("ti_service")


class TiError(ValueError):
    pass


def admin_view(admin: dict) -> dict:
    return {
        "id": admin["id"],
        "email": admin["email"],
        "username": admin.get("username"),
        "role": normalize_admin_role(admin.get("role"), admin.get("restaurant_id")),
        "restaurant_id": admin.get("restaurant_id"),
        "restaurant_name": admin.get("restaurant_name"),
        "must_change_password": admin.get("must_change_password", False),
        "password_pending": not admin.get("password_hash"),
        "last_login_at": admin.get("last_login_at"),
        "created_at": admin.get("created_at"),
    }


class TiService:
    def __init__(self, db, auth_service: AuthService):
        self.restaurant_repo = RestaurantRepository(db)
        self.admin_repo = AdminUserRepository(db)
        self.kds_repo = KdsUserRepository(db)
        self.menu_repo = MenuRepository(db)
        self.auth_service = auth_service

    async def list_restaurants(self) -> list[dict]:
        restaurants = await self.restaurant_repo.list_all()
        admins = await self.admin_repo.list_admins()
        stations = await self.kds_repo.find_for_restaurant(None)
        result = []
        for r in restaurants:
            result.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "location": r.get("location"),
                    "active": r.get("active", True),
                    "opening_hours": normalize_opening_hours(r.get("opening_hours")),
                    "admins": sum(1 for a in admins if a.get("restaurant_id") == r["id"]),
                    "kds_users": sum(1 for s in stations if s.get("restaurant_id") == r["id"]),
                    "payment_gateway_configured": payment_gateway_configured(r),
                }
            )
        return result

    async def list_admins(self, restaurant_id: str | None) -> list[dict]:
        admins = await self.admin_repo.list_admins(restaurant_id)
        if restaurant_id is None:
            admins = [a for a in admins if a.get("restaurant_id")]
        return [admin_view(a) for a in admins]

    async def send_password_reset(self, admin_id: str) -> dict:
        try:
            admin = await self.admin_repo.find_by_id(admin_id)
        except ValueError:
            admin = None
        if admin is None:
            raise TiError("Usuario no encontrado.")
        purpose = "welcome" if not admin.get("password_hash") else "reset"
        await self.auth_service.create_password_reset(admin, purpose)
        return {"detail": f"Se envió el enlace a {admin['email']}.", "email": admin["email"], "purpose": purpose}

    async def create_admin(self, data: dict) -> dict:
        """Admin adicional para un restaurante existente."""
        from src.core.security import hash_password

        try:
            restaurant = await self.restaurant_repo.find_by_id(data["restaurant_id"])
        except ValueError:
            restaurant = None
        if restaurant is None:
            raise TiError("Restaurante no encontrado.")
        email = data["email"].strip().lower()
        if await self.admin_repo.find_by_email(email):
            raise TiError(f"Ya existe un usuario con el correo '{email}'.")
        now = utc_now()
        password = data.get("password")
        admin = await self.admin_repo.insert_one(
            {
                "email": email,
                "username": data["username"].strip(),
                "password_hash": hash_password(password) if password else None,
                "restaurant_id": restaurant["id"],
                "restaurant_name": restaurant["name"],
                "role": AdminRole.RESTAURANT_ADMIN.value,
                "must_change_password": True,
                "created_at": now,
                "last_login_at": None,
                "password_changed_at": None,
            }
        )
        email_sent = False
        if not password:
            try:
                await self.auth_service.create_password_reset(admin, "welcome")
                email_sent = True
            except EmailSendError:
                logger.exception("No se pudo enviar el correo de bienvenida a %s.", email)
        return {"admin": admin_view(admin), "email_sent": email_sent}

    async def create_restaurant(self, data: dict) -> dict:
        name = data["restaurant_name"].strip()
        email = data["admin_email"].strip().lower()
        if await self.restaurant_repo.find_by_name(name):
            raise TiError(f"Ya existe un restaurante llamado '{name}'.")
        if await self.admin_repo.find_by_email(email):
            raise TiError(f"Ya existe un usuario con el correo '{email}'.")

        now = utc_now()
        restaurant = await self.restaurant_repo.insert_one(
            {
                "name": name,
                "location": (data.get("location") or "").strip() or None,
                "active": True,
                "opening_hours": DEFAULT_OPENING_HOURS,
                "service_fee": round(float(data.get("service_fee") or 0.0), 2),
                "created_at": now,
            }
        )
        try:
            admin = await self.admin_repo.insert_one(
                {
                    "email": email,
                    "username": data["admin_username"].strip(),
                    "password_hash": None,
                    "restaurant_id": restaurant["id"],
                    "restaurant_name": name,
                    "role": AdminRole.RESTAURANT_ADMIN.value,
                    "must_change_password": True,
                    "created_at": now,
                    "last_login_at": None,
                    "password_changed_at": None,
                }
            )
            await self.menu_repo.insert_one(
                {
                    "restaurant_id": restaurant["id"],
                    "code": "EJEMPLO-1",
                    "name": "Producto de ejemplo",
                    "category": "ejemplo",
                    "description": "Edita o elimina este producto.",
                    "price": 0.0,
                    "available": False,
                    "schedule_type": "always",
                    "day_of_week": None,
                    "serve_date": None,
                    "option_number": None,
                    "meal_period": None,
                    "prep_time_minutes": 5.0,
                    "ready_from": None,
                    "order_cutoff": None,
                    "daily_stock": None,
                    "dine_in_only": False,
                    "modifiers": [],
                    "is_daily_special": False,
                    "deleted": False,
                    "deleted_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        except Exception:
            await self.restaurant_repo.delete_one(restaurant["id"])
            raise

        email_sent = True
        try:
            await self.auth_service.create_password_reset(admin, "welcome")
        except EmailSendError:
            logger.exception("No se pudo enviar el correo de bienvenida a %s.", email)
            email_sent = False
        return {"restaurant": restaurant, "admin": admin_view(admin), "email_sent": email_sent}
