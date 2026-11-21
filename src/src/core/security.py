"""
Seguridad: hashing de contraseñas/PIN, JWT de sesión (admin/KDS), y códigos
de verificación de un solo uso (WhatsApp/correo institucional).

Tres sistemas de autenticación distintos:

1. **WhatsApp (estudiantes)**: NO usa JWT. Su "sesión" es la existencia de
   un documento en `users` con `email_verified=True` — WhatsApp no tiene
   dónde guardar un token del lado del cliente, así que el número ya
   verificado (una vez) hace ese papel. Lo que sí necesita protección es el
   *código OTP de verificación de correo* (6 dígitos, un solo uso, expira a
   los `OTP_CODE_EXPIRE_MINUTES`): `generate_otp_code()` / `hash_otp_code()`.
2. **Administración** (`admin_users`, analítica/Power BI): login real desde
   un navegador con correo + contraseña -> JWT (`create_access_token()` /
   `decode_access_token()`). Contraseñas con `hash_password()`/`verify_password()`.
3. **KDS** (`kds_users`, pantalla de cocina): login con usuario + PIN
   numérico -> mismo JWT que administración (`role="kds"`), pero el secreto
   es un PIN corto, no una contraseña — mismo hash bcrypt por debajo
   (`hash_pin()`/`verify_pin()`, alias semánticos de `hash_password()`/
   `verify_password()`: bcrypt no le importa si el texto es una contraseña
   larga o un PIN de 4-8 dígitos).
"""

import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from src.configuration import settings

# --- Contraseñas (admin_users) y PIN (kds_users) ----------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# Alias semánticos: mismo mecanismo (bcrypt), pero se leen mejor en el
# código de kds_users (ver src/services/auth_service.py::kds_pin_login).
hash_pin = hash_password
verify_pin = verify_password


# --- JWT de sesión (admin/KDS) -----------------------------------------------


def create_access_token(
    subject: str,
    role: str,
    expires_minutes: int | None = None,
    extra_claims: dict | None = None,
) -> str:
    """`subject` es el id (str) del documento en `admin_users` o `kds_users`, según `role`.

    `extra_claims`: `restaurant_id` (admin/KDS) y `tv` (versión de token del KDS).
    """
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.auth.jwt_expire_minutes
    )
    payload = {"sub": subject, "role": role, "exp": expire, **(extra_claims or {})}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.auth.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Devuelve el payload (`sub`, `role`, `exp`) o lanza ValueError si es inválido/expiró."""
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.auth.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise ValueError("El token expiró, inicia sesión de nuevo.") from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError("Token inválido.") from exc


# --- Códigos OTP de un solo uso (verificación de correo institucional) -----


def generate_otp_code(length: int = 6) -> str:
    """Código numérico (ej. '048213') generado con `secrets` (criptográficamente
    seguro, a diferencia de `random`) — se manda por correo en texto plano."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def hash_otp_code(code: str) -> str:
    """Solo el hash (sha256) se guarda en `users.otp_code_hash`, nunca el código
    en texto plano — mismo patrón que un token de reset de contraseña: una
    fuga de la base de datos no expone códigos válidos utilizables."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# --- Tokens de restablecimiento de contraseña (admin_users) -----------------


def generate_reset_token() -> str:
    """Token aleatorio para el enlace de cambio de contraseña (solo se guarda su hash)."""
    return secrets.token_urlsafe(32)


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- Microsoft Entra ID (SSO institucional, alternativa a la verificación por correo) ---


def validate_entra_id_token(id_token: str) -> dict:
    """TODO: validar el id_token emitido por Microsoft Entra ID (OpenID Connect).

    Pendiente hasta que se decida activar el flujo SSO (ver
    src/api/v1/routes/auth.py, endpoints /login y /callback). El flujo de
    verificación por correo (src/services/auth_service.py) ya cubre la
    vinculación WhatsApp <-> cuenta institucional sin depender de esto.
    """
    raise NotImplementedError("Pendiente: validación de token de Entra ID (Módulo A).")
