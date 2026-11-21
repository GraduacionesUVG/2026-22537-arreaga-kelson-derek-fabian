"""
Rutas de autenticación (Módulo A) — tres sistemas de login
independientes que NO deben confundirse en /docs:

- **WhatsApp (estudiantes)**: registro + verificación de correo
  institucional por código OTP de 6 dígitos. No usa JWT: la "sesión" es el
  documento en `users` con `email_verified=True` (ver
  src/services/auth_service.py). `POST /auth/whatsapp/check` es lo que
  llama el bot (`agent/`) antes de contestar cualquier mensaje.
- **Administración** (`admin_users`, analítica/Business Intelligence): correo +
  contraseña -> JWT. `POST /auth/admin/login`.
- **KDS** (`kds_users`, pantalla de cocina): usuario + PIN numérico -> JWT.
  `POST /auth/kds/login`.

`/auth/login` y `/auth/callback` son placeholders del SSO de Microsoft
Entra ID, todavía sin implementar (por ahora no se prioriza) — por diseño devuelven 501.

Flujo de prueba end-to-end (con la API arriba y Mongo conectado):

1. `POST /auth/register` con correo, número, nombre y carné. Si
   `ENVIRONMENT=DEVELOPMENT`, la respuesta trae `debug_otp_code` (no hace
   falta tener SMTP configurado).
2. `POST /auth/verify-otp` con el mismo número y el `debug_otp_code` (o el
   código real que llegó al correo) -> `status: "verified"`.
3. `POST /auth/whatsapp/check` con el número -> confirma `status: "verified"`
   y devuelve `student_id`/`full_name` (esto es lo que llamaría el bot).
4. `POST /auth/admin/login` con `email` + `password` de un usuario creado
   por `scripts/seed_admin_user.py` -> devuelve el JWT en `access_token`.
5. En Swagger: botón **Authorize** (arriba a la derecha) -> pega el
   `access_token` (sin la palabra "Bearer") -> ya autenticado, todos los
   endpoints con candado usan ese token automáticamente.
6. `GET /auth/admin/me` -> confirma que el JWT es válido (`role`, `sub`).
7. `POST /auth/admin/change-password` -> cambia la contraseña y apaga
   `must_change_password`.
8. `POST /auth/kds/login` con `username` + `pin` de un usuario creado por
   `scripts/seed_kds_user.py` -> devuelve el JWT (`role: "kds"`) más
   `username`/`restaurant_name` para que el front los guarde localmente.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from src.api.v1.deps import (
    get_auth_service,
    get_current_admin_user,
    require_internal_api_key_strict,
)
from src.integrations.email_client import EmailSendError
from src.schemas.auth import (
    AdminLoginIn,
    AdminLoginOut,
    ChangePasswordIn,
    CheckRegistrationIn,
    CheckRegistrationOut,
    KdsPinLoginIn,
    KdsPinLoginOut,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    PasswordResetValidateOut,
    RegisterIn,
    RegisterOut,
    VerifyOtpIn,
    VerifyOtpOut,
)
from src.services.auth_service import (
    AuthService,
    InvalidCredentialsError,
    InvalidTokenError,
    RateLimitExceeded,
    RegistrationConflict,
)

router = APIRouter()


# --- WhatsApp / estudiantes: registro + verificación por OTP ----------------


@router.post("/register", response_model=RegisterOut)
async def register(
    data: RegisterIn,
    service: AuthService = Depends(get_auth_service),
):
    """Paso 1: el estudiante llena el formulario web (número + correo
    institucional) -> se guarda como no verificado y se manda el código OTP.

    También sirve para reenviar el código (mandar el mismo formulario otra
    vez): rate limit de `MAX_VERIFICATION_REQUESTS` (default 3) solicitudes
    por número dentro de `VERIFICATION_REQUEST_WINDOW_HOURS` (default 24h).

    Si el correo ya está verificado y vinculado a OTRO número, responde 409
    con `existing_number_masked`; el front debe preguntarle al estudiante si
    quiere mover su cuenta a este número nuevo y, si confirma, volver a
    llamar este mismo endpoint con `confirm_replace: true`.
    """
    try:
        return await service.register(data)
    except RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except RegistrationConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": str(exc),
                "existing_number_masked": exc.existing_number_masked,
                "hint": "Si el estudiante confirma, reenvía este mismo request con confirm_replace=true.",
            },
        ) from exc
    except EmailSendError as exc:
        # La cuenta/OTP ya se guardó en Mongo (ver AuthService.register) --
        # esto solo significa que Resend no pudo mandar el correo (dominio
        # no verificado, API key inválida, etc). El estudiante puede pedir
        # que se lo reenvíen una vez esté resuelto.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"No se pudo enviar el correo de verificación: {exc}",
        ) from exc


@router.post("/verify-otp", response_model=VerifyOtpOut)
async def verify_otp(data: VerifyOtpIn, service: AuthService = Depends(get_auth_service)):
    """Paso 2: el estudiante escribe en la página el código de 6 dígitos que le llegó al correo."""
    try:
        return await service.verify_otp(data.whatsapp_number, data.otp_code)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post(
    "/whatsapp/check",
    response_model=CheckRegistrationOut,
    dependencies=[Depends(require_internal_api_key_strict)],
    summary="Lo llama el bot (agent/), no un usuario final",
)
async def check_registration(data: CheckRegistrationIn, service: AuthService = Depends(get_auth_service)):
    """El bot de WhatsApp llama esto ANTES de contestar cualquier mensaje (y
    de nuevo cuando el estudiante escribe algo como "ya me registré").

    Protegido con el header `X-Internal-Api-Key` si `INTERNAL_API_KEY` está
    configurado en .env (ver agent/webhook.py).
    """
    return await service.check_registration(data.whatsapp_number)


# --- Administración: correo + contraseña -> JWT ------------------------------


@router.post("/admin/login", response_model=AdminLoginOut, summary="Login de administración/analítica")
async def admin_login(data: AdminLoginIn, service: AuthService = Depends(get_auth_service)):
    """Crea un usuario de prueba primero con `scripts/seed_admin_user.py`."""
    try:
        return await service.admin_login(data.email, data.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


@router.post("/admin/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def admin_change_password(
    data: ChangePasswordIn,
    current_admin: dict = Depends(get_current_admin_user),
    service: AuthService = Depends(get_auth_service),
):
    """Requiere `Authorization: Bearer <access_token>` del login anterior."""
    try:
        await service.admin_change_password(current_admin["sub"], data.current_password, data.new_password)
    except InvalidCredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


@router.get("/admin/me")
async def admin_me(current_admin: dict = Depends(get_current_admin_user)):
    """Endpoint de prueba: confirma que el JWT es válido y qué `role`/`sub` trae."""
    return current_admin


# --- Restablecimiento de contraseña (panel de administración) ----------------


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED, summary="Olvidé mi contraseña")
async def password_reset_request(data: PasswordResetRequestIn, service: AuthService = Depends(get_auth_service)):
    """Envía el enlace de cambio de contraseña si el correo existe. Siempre responde 202."""
    try:
        await service.request_password_reset(data.email)
    except EmailSendError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No se pudo enviar el correo, intenta más tarde.") from exc
    return {"detail": "Si el correo está registrado, recibirás un enlace para cambiar tu contraseña."}


@router.get("/password-reset/validate", response_model=PasswordResetValidateOut)
async def password_reset_validate(token: str, service: AuthService = Depends(get_auth_service)):
    """Verifica que el token del enlace siga vigente antes de mostrar el formulario."""
    try:
        return await service.validate_password_reset(token)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def password_reset_confirm(data: PasswordResetConfirmIn, service: AuthService = Depends(get_auth_service)):
    """Define la nueva contraseña con el token del enlace (un solo uso)."""
    try:
        await service.confirm_password_reset(data.token, data.new_password)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# --- KDS: usuario + PIN numérico -> JWT ---------------------------------------


@router.post("/kds/login", response_model=KdsPinLoginOut, summary="Login rápido del KDS (usuario + PIN)")
async def kds_login(data: KdsPinLoginIn, service: AuthService = Depends(get_auth_service)):
    """Crea un usuario de prueba primero con `scripts/seed_kds_user.py`.

    El front debe guardar localmente `username`/`kds_user_id`/`restaurant_name`
    de la respuesta: en la siguiente visita, solo pide el PIN y reenvía el
    `username` ya guardado a este mismo endpoint (login rápido)."""
    try:
        return await service.kds_pin_login(data.username, data.pin)
    except InvalidCredentialsError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


# --- SSO Microsoft Entra ID (alternativa institucional) ---
# Placeholders: no implementado, y por ahora no se prioriza para la demo. El
# flujo de verificación por OTP de arriba ya cubre la vinculación WhatsApp
# <-> cuenta institucional sin depender de esto.


@router.get("/login", summary="[No implementado] SSO Entra ID — no es el login de admin/KDS")
async def login():
    """GET /auth/login — redirige al flujo OAuth2 de Microsoft Entra ID.

    Placeholder, siempre responde 501. ¿Buscabas loguearte? Es
    `POST /auth/admin/login` o `POST /auth/kds/login`.
    """
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Pendiente: integración SSO Entra ID.")


@router.get("/callback", summary="[No implementado] SSO Entra ID")
async def callback(code: str | None = None):
    """GET /auth/callback — recibe el JWT de Microsoft y vincula la cuenta al carné/WhatsApp."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Pendiente: integración SSO Entra ID.")
