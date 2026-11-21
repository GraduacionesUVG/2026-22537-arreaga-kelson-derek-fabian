"""
Schemas de la API para los tres flujos de autenticación: verificación de WhatsApp por OTP de correo, login
de administración (correo+contraseña) y login de KDS (usuario+PIN).
"""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

INSTITUTIONAL_EMAIL_DOMAIN = "@uvg.edu.gt"


# --- WhatsApp / estudiantes -------------------------------------------------


class RegisterIn(BaseModel):
    # El front antepone el prefijo de país (502) antes de mandarlo -- así
    # llega en formato E.164, que es como lo identifica la API de Meta.
    whatsapp_number: str = Field(
        pattern=r"^\+502\d{8}$",
        description="Formato E.164 con prefijo de Guatemala, ej. +50255551234 (el front antepone el 502).",
    )
    full_name: str
    email: EmailStr = Field(description="Correo institucional, debe terminar en @uvg.edu.gt.")
    student_id: str = Field(description="Carné del estudiante.")
    confirm_replace: bool = Field(
        False,
        description=(
            "Solo se manda en `true` cuando una respuesta previa de este mismo endpoint "
            "devolvió status='conflict' y el estudiante confirmó que quiere mover ese "
            "correo a este número nuevo."
        ),
    )

    @field_validator("email")
    @classmethod
    def _validate_institutional_email(cls, value: EmailStr) -> EmailStr:
        if not str(value).lower().endswith(INSTITUTIONAL_EMAIL_DOMAIN):
            raise ValueError(f"Debe ser un correo institucional ({INSTITUTIONAL_EMAIL_DOMAIN}).")
        return value


class RegisterOut(BaseModel):
    """Respuesta de éxito (200) de POST /auth/register. El caso de correo en
    conflicto con otro número responde 409 con un cuerpo distinto (`detail`,
    `existing_number_masked`, `hint`), no pasa por este schema -- ver el
    docstring del endpoint en src/api/v1/routes/auth.py."""

    status: str = "sent"
    detail: str
    expires_in_minutes: int
    # Solo se llena si ENVIRONMENT=DEVELOPMENT, para probar sin un correo real.
    debug_otp_code: str | None = None


class VerifyOtpIn(BaseModel):
    whatsapp_number: str = Field(pattern=r"^\+502\d{8}$")
    otp_code: str = Field(pattern=r"^\d{6}$", description="Código de 6 dígitos enviado por correo.")


class VerifyOtpOut(BaseModel):
    status: str  # "verified"
    message: str


class CheckRegistrationIn(BaseModel):
    whatsapp_number: str = Field(
        pattern=r"^\+502\d{8}$",
        description="wa_id del remitente tal como lo manda el webhook de Meta (con prefijo 502).",
    )


class CheckRegistrationOut(BaseModel):
    status: str  # "not_registered" | "pending_verification" | "verified"
    message: str
    login_url: str | None = None  # se llena si status != "verified"
    student_id: str | None = None  # se llena si status == "verified"
    full_name: str | None = None  # se llena si status == "verified"


# --- Administración (admin_users): correo + contraseña -> JWT ---------------


class AdminLoginIn(BaseModel):
    email: EmailStr
    password: str


class AdminLoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = Field(description="restaurant_admin | superadmin")
    must_change_password: bool
    restaurant_id: str | None = None
    restaurant_name: str | None = None
    username: str | None = None


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class PasswordResetValidateOut(BaseModel):
    valid: bool
    email: str
    purpose: str = Field(description="reset | welcome")
    expires_at: datetime


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


# --- KDS (kds_users): usuario + PIN numérico -> JWT --------------------------


class KdsPinLoginIn(BaseModel):
    username: str
    pin: str = Field(pattern=r"^\d{4,8}$", description="Solo números, entre 4 y 8 dígitos.")


class KdsPinLoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = "kds"
    # El front guarda estos campos localmente (ej. localStorage) para que,
    # en la siguiente visita, solo tenga que pedir el PIN.
    kds_user_id: str
    username: str
    restaurant_name: str
    restaurant_id: str | None = None
