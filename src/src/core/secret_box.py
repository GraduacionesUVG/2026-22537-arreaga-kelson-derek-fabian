"""Cifrado simétrico (Fernet) de secretos que se guardan en Mongo.

Hoy lo usan las credenciales de Tilopay de cada restaurante
(src/services/payment_accounts.py). La clave sale de
`PAYMENT_CREDENTIALS_SECRET` (cualquier texto: se deriva con SHA-256). En
PRODUCTION es obligatoria; en desarrollo, si falta, se deriva de
`JWT_SECRET_KEY` para no frenar las pruebas.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from src.configuration import settings


class SecretBoxError(Exception):
    """No hay clave configurada, o el valor guardado no se puede descifrar con la clave actual."""


def _fernet() -> Fernet:
    secret = settings.tilopay.credentials_secret
    if not secret:
        if settings.environment.upper() == "PRODUCTION":
            raise SecretBoxError("PAYMENT_CREDENTIALS_SECRET no está configurado en el backend.")
        secret = f"dev-only:{settings.jwt_secret_key}"
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretBoxError(
            "No se pudieron descifrar las credenciales guardadas (¿cambió PAYMENT_CREDENTIALS_SECRET?)."
        ) from exc
