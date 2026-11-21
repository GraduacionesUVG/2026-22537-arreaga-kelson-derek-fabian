"""
Cliente de envío de correo (API HTTP de Resend) para el código OTP de
verificación de WhatsApp (src/services/auth_service.py).

Se usa la API HTTP de Resend (https://api.resend.com/emails), no SMTP: los
planes free de Render bloquean el trafico saliente a los puertos SMTP
(25/465/587) desde el 26 de septiembre de 2025 -- ver
https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports.
La API HTTP usa HTTPS normal (puerto 443), que nunca se bloquea, y de paso
es nativamente async (no hace falta correrla en un thread aparte).

Modo desarrollo: si `settings.email.resend_api_key` está vacío (default en
.env.example), no falla -- imprime el correo en consola. Así se puede
probar el flujo completo de `POST /auth/register` sin credenciales reales;
además esa misma respuesta ya trae `debug_otp_code` cuando
ENVIRONMENT=DEVELOPMENT.

En producción se necesita una cuenta de Resend con el dominio verificado y su API key.
"""

import httpx

from src.configuration import settings

RESEND_API_URL = "https://api.resend.com/emails"


class EmailSendError(Exception):
    """Resend devolvió un error al intentar mandar el correo."""


async def send_email(to: str, subject: str, body: str) -> None:
    if not settings.email.resend_api_key:
        print(f"\n[email_client] (modo dev, sin RESEND_API_KEY configurado)\nPara: {to}\nAsunto: {subject}\n{body}\n")
        return

    payload = {
        "from": settings.email.from_address,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    headers = {"Authorization": f"Bearer {settings.email.resend_api_key}"}

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(RESEND_API_URL, json=payload, headers=headers)

    if response.status_code >= 400:
        raise EmailSendError(f"Resend devolvió {response.status_code}: {response.text}")


async def send_otp_email(to: str, full_name: str, otp_code: str, expires_minutes: int) -> None:
    subject = "Tu código de verificación - Caja Virtual Infinita"
    body = (
        f"Hola {full_name},\n\n"
        f"Tu código de verificación es:\n\n"
        f"    {otp_code}\n\n"
        f"Escríbelo en la página de registro para confirmar tu cuenta. Es válido "
        f"por {expires_minutes} minutos.\n\n"
        "Si no solicitaste esto, ignora este correo."
    )
    await send_email(to, subject, body)


async def send_password_reset_email(
    to: str,
    name: str,
    url: str,
    expires_minutes: int,
    welcome: bool = False,
    restaurant_name: str | None = None,
) -> None:
    if welcome:
        subject = "Crea tu contraseña - Panel de administración"
        intro = (
            f"Se creó tu acceso al panel de administración"
            f"{' de ' + restaurant_name if restaurant_name else ''}.\n"
            "Para definir tu contraseña, abre este enlace:"
        )
    else:
        subject = "Cambio de contraseña - Panel de administración"
        intro = "Recibimos una solicitud para cambiar tu contraseña. Para definir una nueva, abre este enlace:"
    body = (
        f"Hola {name},\n\n"
        f"{intro}\n\n"
        f"    {url}\n\n"
        f"El enlace es válido por {expires_minutes} minutos y solo se puede usar una vez.\n\n"
        "Si no solicitaste esto, ignora este correo."
    )
    await send_email(to, subject, body)
