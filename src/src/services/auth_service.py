"""
Servicio de autenticación — tres flujos independientes:

1. **WhatsApp (estudiantes)**: verificación de correo institucional por
   código OTP de 6 dígitos. `register` -> `verify_otp`. No hay JWT: la
   "sesión" es el documento en `users` con `email_verified=True`.
   `check_registration` es lo que llama `agent/` (el bot) antes de
   contestar cualquier mensaje.
2. **Administración** (`admin_users`, analítica): `admin_login` /
   `admin_change_password`, con JWT (ver src/core/security.py).
3. **KDS** (`kds_users`, pantalla de cocina): `kds_pin_login`, usuario + PIN
   numérico, también con JWT (mismo mecanismo, distinto secreto/colección).

Flujo completo (para probar en /docs):
    POST /auth/register          -> registra (o reintenta) y manda el código OTP
                                     (en ENVIRONMENT=DEVELOPMENT vuelve en debug_otp_code)
    POST /auth/verify-otp        -> confirma el código, marca email_verified=True
    POST /auth/whatsapp/check    -> lo llama el bot antes de cada respuesta
    POST /auth/admin/login       -> correo+contraseña -> JWT (role=admin)
    POST /auth/kds/login         -> usuario+PIN -> JWT (role=kds)
"""

from datetime import datetime, timedelta, timezone

from src.configuration import settings
from src.core.security import (
    create_access_token,
    generate_otp_code,
    generate_reset_token,
    hash_otp_code,
    hash_password,
    hash_reset_token,
    verify_password,
    verify_pin,
)
from src.integrations import email_client
from src.models.admin_user import normalize_admin_role
from src.repositories.admin_user_repo import AdminUserRepository
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.password_reset_repo import PasswordResetRepository
from src.repositories.user_repo import UserRepository
from src.schemas.auth import RegisterIn


class RateLimitExceeded(Exception):
    """Se superó MAX_VERIFICATION_REQUESTS dentro de VERIFICATION_REQUEST_WINDOW_HOURS."""


class InvalidTokenError(Exception):
    """Código OTP inexistente, ya usado, expirado, o incorrecto."""


class InvalidCredentialsError(Exception):
    """Usuario/contraseña (o usuario/PIN) incorrectos, o contraseña actual incorrecta al cambiarla."""


class RegistrationConflict(Exception):
    """El correo ya está verificado y vinculado a OTRO número de WhatsApp.

    `existing_number_masked` se le muestra al estudiante; si confirma que
    quiere mover su cuenta a este número nuevo, se reintenta
    `AuthService.register` con `confirm_replace=True`.
    """

    def __init__(self, existing_number_masked: str):
        self.existing_number_masked = existing_number_masked
        super().__init__("Este correo ya está vinculado a otro número de WhatsApp.")


def mask_phone_number(number: str, visible: int = 4) -> str:
    """Enmascara todo menos los últimos `visible` caracteres (ej. '+50255551234' -> 'XXXXXXXX1234')."""
    if len(number) <= visible:
        return number
    return "X" * (len(number) - visible) + number[-visible:]


class AuthService:
    def __init__(
        self,
        user_repo: UserRepository,
        admin_repo: AdminUserRepository,
        kds_repo: KdsUserRepository,
        db=None,
    ):
        self.user_repo = user_repo
        self.admin_repo = admin_repo
        self.kds_repo = kds_repo
        self.reset_repo = PasswordResetRepository(db) if db is not None else None

    # --- WhatsApp / estudiantes ---------------------------------------------

    async def register(self, data: RegisterIn) -> dict:
        """Paso 1: el estudiante llena el formulario web -> se guarda (o
        actualiza) la cuenta como no verificada y se manda el código OTP.

        A diferencia del diseño anterior (colección `pending_registrations`
        aparte), el documento se escribe directo en `users` desde el primer
        intento -- así un número "a medio registrar" ya es consultable por
        `check_registration` como `pending_verification`, no como
        `not_registered`.
        """
        now = datetime.now(timezone.utc)

        by_email = await self.user_repo.find_by_email(data.email)
        by_whatsapp = await self.user_repo.find_by_whatsapp_number(data.whatsapp_number)

        target = None
        if by_email and by_email["whatsapp_number"] != data.whatsapp_number:
            if by_email["email_verified"] and not data.confirm_replace:
                raise RegistrationConflict(mask_phone_number(by_email["whatsapp_number"]))
            # Correo no verificado vinculado a otro número, o el estudiante ya
            # confirmó el reemplazo: este intento nuevo gana el correo.
            target = by_email
            if by_whatsapp and by_whatsapp["id"] != by_email["id"]:
                # Ese whatsapp_number ya tenía su propio intento (con otro
                # correo) -- se descarta, un número no puede tener dos cuentas.
                await self.user_repo.delete_one(by_whatsapp["id"])
        elif by_whatsapp:
            target = by_whatsapp

        request_count, window_started_at = self._next_rate_limit_window(target, now)

        otp_code = generate_otp_code()
        otp_expires_at = now + timedelta(minutes=settings.auth.otp_code_expire_minutes)

        doc = {
            "whatsapp_number": data.whatsapp_number,
            "full_name": data.full_name,
            "email": data.email,
            "student_id": data.student_id,
            "email_verified": False,
            "otp_code_hash": hash_otp_code(otp_code),
            "otp_expires_at": otp_expires_at,
            "otp_attempts": 0,
            "otp_request_count": request_count,
            "otp_window_started_at": window_started_at,
        }

        if target:
            await self.user_repo.update_one(target["id"], doc)
        else:
            doc["created_at"] = now
            doc["last_interaction_at"] = now
            await self.user_repo.insert_one(doc)

        await email_client.send_otp_email(
            to=data.email,
            full_name=data.full_name,
            otp_code=otp_code,
            expires_minutes=settings.auth.otp_code_expire_minutes,
        )

        response = {
            "status": "sent",
            "detail": "Se envió un código de verificación a tu correo institucional.",
            "expires_in_minutes": settings.auth.otp_code_expire_minutes,
        }
        if settings.environment == "DEVELOPMENT":
            # Solo en desarrollo: evita depender de un servidor de correo real para probar el flujo.
            response["debug_otp_code"] = otp_code
        return response

    def _next_rate_limit_window(self, existing: dict | None, now: datetime) -> tuple[int, datetime]:
        """Cuenta reenvíos dentro de `VERIFICATION_REQUEST_WINDOW_HOURS`. Ya no
        depende de un índice TTL para "resetear" la ventana (no hay una
        colección aparte que Mongo pueda borrar sola): si la ventana anterior
        ya venció, simplemente arranca una nueva en 1."""
        if not existing or existing.get("email_verified"):
            return 1, now

        window_started_at = existing.get("otp_window_started_at")
        window_hours = settings.auth.verification_request_window_hours
        if window_started_at is not None:
            window_started_at = window_started_at if window_started_at.tzinfo else window_started_at.replace(tzinfo=timezone.utc)
            if window_started_at + timedelta(hours=window_hours) > now:
                request_count = existing.get("otp_request_count", 0)
                if request_count >= settings.auth.max_verification_requests:
                    raise RateLimitExceeded(
                        f"Se alcanzó el máximo de {settings.auth.max_verification_requests} solicitudes. "
                        f"Intenta de nuevo en {window_hours} horas."
                    )
                return request_count + 1, window_started_at

        return 1, now

    async def verify_otp(self, whatsapp_number: str, otp_code: str) -> dict:
        """Paso 2: el estudiante escribe el código que le llegó por correo."""
        user = await self.user_repo.find_by_whatsapp_number(whatsapp_number)
        if user is None:
            raise InvalidTokenError("No hay ningún registro en curso para este número. Regístrate primero.")

        if user["email_verified"]:
            return {"status": "verified", "message": "Esta cuenta ya estaba verificada."}

        now = datetime.now(timezone.utc)
        expires_at = user.get("otp_expires_at")
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if not user.get("otp_code_hash") or expires_at is None or expires_at < now:
            raise InvalidTokenError("El código expiró o no existe. Solicita uno nuevo.")

        if user.get("otp_attempts", 0) >= settings.auth.max_otp_attempts:
            raise InvalidTokenError("Demasiados intentos con este código. Solicita uno nuevo.")

        if hash_otp_code(otp_code) != user["otp_code_hash"]:
            await self.user_repo.update_one(user["id"], {"otp_attempts": user.get("otp_attempts", 0) + 1})
            raise InvalidTokenError("Código incorrecto.")

        await self.user_repo.update_one(
            user["id"],
            {
                "email_verified": True,
                "otp_code_hash": None,
                "otp_expires_at": None,
                "otp_attempts": 0,
                "last_interaction_at": now,
            },
        )
        return {"status": "verified", "message": "Cuenta verificada correctamente."}

    async def check_registration(self, whatsapp_number: str) -> dict:
        """Lo llama `agent/` (el bot) antes de contestar cualquier mensaje,
        y también cuando el estudiante escribe algo como "ya me registré"."""
        user = await self.user_repo.find_by_whatsapp_number(whatsapp_number)

        if user is None:
            return {
                "status": "not_registered",
                "message": "Todavía no estás registrado. Regístrate aquí:",
                "login_url": settings.auth.prod_login_url,
            }

        if not user["email_verified"]:
            return {
                "status": "pending_verification",
                "message": "Registraste tu cuenta pero tu correo institucional no está verificado todavía.",
                "login_url": settings.auth.prod_login_url,
            }

        await self.user_repo.touch_last_interaction(user["id"])
        return {
            "status": "verified",
            "message": "Cuenta verificada.",
            "student_id": user["student_id"],
            "full_name": user["full_name"],
        }

    # --- Administración (admin_users) --------------------------------------

    async def admin_login(self, email: str, password: str) -> dict:
        admin = await self.admin_repo.find_by_email(email)
        if admin is None or not admin.get("password_hash") or not verify_password(password, admin["password_hash"]):
            raise InvalidCredentialsError("Correo o contraseña incorrectos.")

        role = normalize_admin_role(admin.get("role"), admin.get("restaurant_id"))
        update = {"last_login_at": datetime.now(timezone.utc)}
        if role != admin.get("role"):
            update["role"] = role
        await self.admin_repo.update_one(admin["id"], update)
        token = create_access_token(
            subject=admin["id"], role=role, extra_claims={"restaurant_id": admin.get("restaurant_id")}
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "role": role,
            "must_change_password": admin.get("must_change_password", False),
            "restaurant_id": admin.get("restaurant_id"),
            "restaurant_name": admin.get("restaurant_name"),
            "username": admin.get("username"),
        }

    async def admin_change_password(self, admin_id: str, current_password: str, new_password: str) -> None:
        admin = await self.admin_repo.find_by_id(admin_id)
        if admin is None or not admin.get("password_hash") or not verify_password(current_password, admin["password_hash"]):
            raise InvalidCredentialsError("La contraseña actual no es correcta.")

        await self.admin_repo.update_one(
            admin_id,
            {
                "password_hash": hash_password(new_password),
                "must_change_password": False,
                "password_changed_at": datetime.now(timezone.utc),
            },
        )

    # --- Restablecimiento de contraseña (admin_users) -------------------------

    async def create_password_reset(self, admin: dict, purpose: str = "reset") -> None:
        """Genera un token de un solo uso y envía el enlace por correo.

        `purpose`: `reset` (cambio de contraseña) o `welcome` (contraseña inicial).
        """
        now = datetime.now(timezone.utc)
        await self.reset_repo.invalidate_for_admin(admin["id"], now)
        token = generate_reset_token()
        await self.reset_repo.insert_one(
            {
                "admin_id": admin["id"],
                "token_hash": hash_reset_token(token),
                "purpose": purpose,
                "created_at": now,
                "expires_at": now + timedelta(minutes=settings.auth.password_reset_expire_minutes),
                "used_at": None,
            }
        )
        await email_client.send_password_reset_email(
            to=admin["email"],
            name=admin.get("username") or admin["email"],
            url=f"{settings.auth.admin_frontend_url.rstrip('/')}/reset-password?token={token}",
            expires_minutes=settings.auth.password_reset_expire_minutes,
            welcome=purpose == "welcome",
            restaurant_name=admin.get("restaurant_name"),
        )

    async def request_password_reset(self, email: str) -> None:
        """"Olvidé mi contraseña": si el correo no existe no hace nada (no revela usuarios)."""
        admin = await self.admin_repo.find_by_email(email)
        if admin is not None:
            await self.create_password_reset(admin)

    async def _valid_reset(self, token: str) -> tuple[dict, dict]:
        record = await self.reset_repo.find_by_hash(hash_reset_token(token))
        now = datetime.now(timezone.utc)
        if record is None or record.get("used_at") is not None:
            raise InvalidTokenError("El enlace no es válido o ya se usó. Solicita uno nuevo.")
        expires_at = record["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            raise InvalidTokenError("El enlace expiró. Solicita uno nuevo.")
        admin = await self.admin_repo.find_by_id(record["admin_id"])
        if admin is None:
            raise InvalidTokenError("El usuario ya no existe.")
        return record, admin

    async def validate_password_reset(self, token: str) -> dict:
        record, admin = await self._valid_reset(token)
        return {
            "valid": True,
            "email": admin["email"],
            "purpose": record.get("purpose", "reset"),
            "expires_at": record["expires_at"],
        }

    async def confirm_password_reset(self, token: str, new_password: str) -> None:
        record, admin = await self._valid_reset(token)
        now = datetime.now(timezone.utc)
        await self.admin_repo.update_one(
            admin["id"],
            {"password_hash": hash_password(new_password), "must_change_password": False, "password_changed_at": now},
        )
        await self.reset_repo.update_one(record["id"], {"used_at": now})

    # --- KDS (kds_users) ------------------------------------------------------

    async def kds_pin_login(self, username: str, pin: str) -> dict:
        """Login rápido del KDS: usuario + PIN numérico. La respuesta trae
        `kds_user_id`/`username`/`restaurant_name` para que el front los
        guarde localmente y, en la siguiente visita, solo pida el PIN
        (reenviando el mismo `username` guardado a este mismo endpoint)."""
        kds_user = await self.kds_repo.find_by_username(username)
        if kds_user is None or not verify_pin(pin, kds_user["pin_hash"]):
            raise InvalidCredentialsError("Usuario o PIN incorrectos.")

        await self.kds_repo.update_one(kds_user["id"], {"last_login_at": datetime.now(timezone.utc)})
        token = create_access_token(
            subject=kds_user["id"],
            role="kds",
            extra_claims={"restaurant_id": kds_user.get("restaurant_id"), "tv": int(kds_user.get("token_version", 0))},
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "role": "kds",
            "kds_user_id": kds_user["id"],
            "username": kds_user["username"],
            "restaurant_name": kds_user["restaurant_name"],
            "restaurant_id": kds_user.get("restaurant_id"),
        }
