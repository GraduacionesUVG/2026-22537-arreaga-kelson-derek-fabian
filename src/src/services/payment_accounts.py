"""Cuenta de Tilopay de cada restaurante.

Cada restaurante cobra en su propia cuenta de Tilopay (el dinero y la
factura son del restaurante, no de la plataforma). Las credenciales se
cargan desde el panel (`/api/admin/restaurant/payment-gateway`) y se guardan
en el documento del restaurante:

    "payment_gateway": {
        "provider": "tilopay",
        "api_user": "...",                 # no es secreto
        "api_password_enc": "<Fernet>",    # cifrado con PAYMENT_CREDENTIALS_SECRET
        "api_key_enc": "<Fernet>",
        "api_key_last4": "3133",           # solo para mostrar en el panel
        "currency": "GTQ",
        "verified_at": ISODate,            # último login exitoso contra Tilopay
        "updated_at": ISODate,
        "updated_by": {"type", "id", "username"}
    }

La contraseña y la API key nunca se devuelven por la API.
"""

import logging

from src.configuration import settings
from src.core import secret_box
from src.core.secret_box import SecretBoxError
from src.core.timeutils import utc_now
from src.integrations import tilopay_client
from src.integrations.tilopay_client import TilopayCredentials
from src.repositories.restaurant_repo import RestaurantRepository

logger = logging.getLogger("payments")

FIELD = "payment_gateway"
PROVIDER_TILOPAY = "tilopay"
SUPPORTED_CURRENCIES = ("GTQ", "USD")


class PaymentAccountError(Exception):
    """La cuenta no se pudo guardar o leer (credenciales rechazadas, clave de cifrado, etc.)."""


def gateway_view(restaurant: dict) -> dict:
    """Estado de la cuenta de pagos para el panel (sin secretos)."""
    gateway = restaurant.get(FIELD) or {}
    configured = gateway.get("provider") == PROVIDER_TILOPAY and bool(gateway.get("api_password_enc"))
    return {
        "provider": PROVIDER_TILOPAY,
        "configured": configured,
        "api_user": gateway.get("api_user") if configured else None,
        "api_key_last4": gateway.get("api_key_last4") if configured else None,
        "currency": gateway.get("currency") if configured else None,
        "verified_at": gateway.get("verified_at") if configured else None,
        "updated_at": gateway.get("updated_at") if configured else None,
    }


def is_configured(restaurant: dict | None) -> bool:
    return bool(restaurant) and gateway_view(restaurant)["configured"]


class PaymentAccountService:
    def __init__(self, restaurant_repo: RestaurantRepository):
        self.repo = restaurant_repo

    async def get_credentials(self, restaurant_id: str) -> TilopayCredentials | None:
        """Credenciales descifradas del restaurante, o None si no tiene cuenta configurada.

        Lanza PaymentAccountError si hay cuenta pero no se puede descifrar.
        """
        try:
            restaurant = await self.repo.find_by_id(restaurant_id)
        except ValueError:
            restaurant = None
        if not is_configured(restaurant):
            return None
        gateway = restaurant[FIELD]
        try:
            return TilopayCredentials(
                api_user=gateway["api_user"],
                api_password=secret_box.decrypt(gateway["api_password_enc"]),
                api_key=secret_box.decrypt(gateway["api_key_enc"]),
                currency=gateway.get("currency") or settings.tilopay.default_currency,
            )
        except SecretBoxError as exc:
            logger.error("Cuenta de Tilopay del restaurante %s ilegible: %s", restaurant_id, exc)
            raise PaymentAccountError(
                "Las credenciales de Tilopay del restaurante no se pueden leer. Vuelve a cargarlas en el panel."
            ) from exc

    async def save_tilopay(
        self, restaurant_id: str, *, api_user: str, api_password: str, api_key: str, currency: str, actor: dict | None
    ) -> dict:
        """Verifica las credenciales con Tilopay (login) y las guarda cifradas.

        Lanza TilopayAuthError si Tilopay las rechaza, TilopayError si no responde y
        PaymentAccountError si falta la clave de cifrado.
        """
        creds = TilopayCredentials(
            api_user=api_user.strip(), api_password=api_password, api_key=api_key.strip(), currency=currency
        )
        try:
            password_enc, key_enc = secret_box.encrypt(creds.api_password), secret_box.encrypt(creds.api_key)
        except SecretBoxError as exc:
            raise PaymentAccountError(str(exc)) from exc
        await tilopay_client.verify_credentials(creds)
        now = utc_now()
        gateway = {
            "provider": PROVIDER_TILOPAY,
            "api_user": creds.api_user,
            "api_password_enc": password_enc,
            "api_key_enc": key_enc,
            "api_key_last4": creds.api_key[-4:],
            "currency": creds.currency,
            "verified_at": now,
            "updated_at": now,
            "updated_by": _actor_summary(actor),
        }
        updated = await self.repo.update_one(restaurant_id, {FIELD: gateway})
        logger.info("Cuenta de Tilopay del restaurante %s actualizada (api_user=%s).", restaurant_id, creds.api_user)
        return updated

    async def clear(self, restaurant_id: str) -> dict | None:
        return await self.repo.update_one(restaurant_id, {FIELD: None})


def _actor_summary(actor: dict | None) -> dict | None:
    if not actor:
        return None
    return {"type": actor.get("type") or actor.get("role"), "id": actor.get("id"), "username": actor.get("username")}
