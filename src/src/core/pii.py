"""Cifrado de datos personales de estudiantes (`users`).

Cómo se guarda el número (ver src/repositories/user_repo.py):

- `whatsapp_number_enc`: el número cifrado con Fernet (AES-128-CBC + HMAC).
  Cada cifrado sale distinto, así que no sirve para buscar.
- `whatsapp_number_hash`: HMAC-SHA256 del número normalizado (solo dígitos).
  Es determinista: sirve para buscar e indexar como único sin guardar el
  número. Al ser un HMAC con clave, no se puede revertir probando los ~10^8
  números de Guatemala sin conocer la clave.

Las dos claves salen de `USER_DATA_SECRET` (cualquier texto largo y
aleatorio) con contextos distintos. En PRODUCTION es obligatoria; en
desarrollo, si falta, se deriva de `JWT_SECRET_KEY`.

Si `USER_DATA_SECRET` cambia o se pierde, los números guardados ya no se
pueden leer ni buscar: los estudiantes tendrían que registrarse de nuevo.
"""

import base64
import hashlib
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken

from src.configuration import settings


class PiiCryptoError(Exception):
    """No hay clave configurada, o el valor guardado no se puede descifrar con la clave actual."""


def _master_secret() -> str:
    secret = settings.user_data_secret
    if not secret:
        if settings.environment.upper() == "PRODUCTION":
            raise PiiCryptoError("USER_DATA_SECRET no está configurado en el backend.")
        secret = f"dev-only-user-data:{settings.jwt_secret_key}"
    return secret


def _derive(purpose: str) -> bytes:
    return hashlib.sha256(f"{purpose}:{_master_secret()}".encode()).digest()


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive("users.encryption.v1")))


def normalize_whatsapp_number(number: str) -> str:
    """Solo dígitos: '+502 5555-1234', '50255551234' y '+50255551234' dan lo mismo."""
    return re.sub(r"\D", "", number or "")


def whatsapp_number_hash(number: str) -> str:
    """Índice ciego para buscar por número sin guardarlo en claro."""
    normalized = normalize_whatsapp_number(number)
    return hmac.new(_derive("users.whatsapp_number.index.v1"), normalized.encode(), hashlib.sha256).hexdigest()


def encrypt_value(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise PiiCryptoError("No se pudo descifrar el dato guardado (¿cambió USER_DATA_SECRET?).") from exc
