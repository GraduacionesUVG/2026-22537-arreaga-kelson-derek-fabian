"""
Servicio de pagos: links de pago de Tilopay (src/integrations/tilopay_client.py).

Cada restaurante cobra en su propia cuenta de Tilopay (ver
src/services/payment_accounts.py). Al aceptar una solicitud se usa la cuenta
del restaurante del pedido:

- **tilopay** (el restaurante tiene cuenta): se crea un link de un solo uso
  en esa cuenta. El pago se confirma consultando a Tilopay (`is_paid`), nunca
  confiando en lo que llega al webhook: Tilopay no publica el algoritmo de su
  `orderHash`, así que el webhook/callback solo sirve de aviso para ir a
  consultar. El webhook además lleva un token HMAC por pedido en la URL.
- **mock** (el restaurante no tiene cuenta y los pagos simulados están
  permitidos, ver `simulated_payments_allowed`): link falso y el pago se
  simula con POST /webhooks/bac-payment.
- Sin cuenta y sin pagos simulados (PRODUCTION): no se puede aceptar la
  solicitud (`PaymentNotConfiguredError`).
"""

import hashlib
import hmac
import logging
import uuid
from dataclasses import dataclass

from src.configuration import settings
from src.integrations import tilopay_client
from src.integrations.tilopay_client import TilopayCredentials, TilopayError
from src.repositories.restaurant_repo import RestaurantRepository
from src.services.payment_accounts import PROVIDER_TILOPAY, PaymentAccountError, PaymentAccountService

logger = logging.getLogger("payments")

PROVIDER_MOCK = "mock"

# Valores con los que Tilopay marca un pago aprobado / no aprobado dentro de
# `payments` (getDetailLinkPayment). TODO(tilopay): confirmar con una respuesta
# real en modo test; la documentación pública no detalla estos registros.
_STATUS_KEYS = ("code", "response_code", "status", "response", "estado")
_APPROVED_VALUES = {"1", "approved", "aprobada", "aprobado", "success", "paid", "pagado"}


class PaymentGatewayError(Exception):
    """No se pudo generar el link de pago en la pasarela."""


class PaymentNotConfiguredError(PaymentGatewayError):
    """El restaurante no tiene una cuenta de Tilopay utilizable y no se permiten pagos simulados."""


@dataclass
class PaymentLink:
    url: str
    provider: str
    link_id: str | None = None
    account_user: str | None = None  # api_user de la cuenta de Tilopay donde se creó el link


def simulated_payments_allowed() -> bool:
    """Pagos simulados para restaurantes sin cuenta: `SIMULATED_PAYMENTS_ENABLED`, o por defecto
    permitidos fuera de PRODUCTION."""
    explicit = settings.tilopay.simulated_payments_enabled
    if explicit is not None:
        return explicit
    return settings.environment.upper() != "PRODUCTION"


def order_webhook_token(order_id: str) -> str:
    """Token por pedido que viaja en la URL del webhook/callback de Tilopay."""
    secret = settings.tilopay.webhook_secret or settings.jwt_secret_key
    return hmac.new(secret.encode(), f"tilopay:{order_id}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_order_webhook_token(order_id: str, token: str) -> bool:
    return hmac.compare_digest(order_webhook_token(order_id), token or "")


def _payment_is_approved(payment) -> bool:
    if not isinstance(payment, dict):
        return False
    for key in _STATUS_KEYS:
        if payment.get(key) is not None:
            return str(payment[key]).strip().lower() in _APPROVED_VALUES
    # Sin campo de estado reconocible: Tilopay lo listó como pago del link.
    logger.warning("Pago de Tilopay sin campo de estado reconocido (campos: %s); se toma como aprobado.", sorted(payment))
    return True


class PaymentService:
    def __init__(self, restaurant_repo: RestaurantRepository):
        self.accounts = PaymentAccountService(restaurant_repo)

    async def request_payment_link(self, order: dict) -> PaymentLink:
        order_id, amount = order["id"], float(order["total_amount"])
        try:
            creds = await self.accounts.get_credentials(order["restaurant_id"])
        except PaymentAccountError as exc:
            raise PaymentNotConfiguredError(str(exc)) from exc

        if creds is None:
            if not simulated_payments_allowed():
                raise PaymentNotConfiguredError(
                    "El restaurante no tiene configurada su cuenta de Tilopay. "
                    "Configúrala en el panel de administración antes de aceptar pedidos."
                )
            ref = uuid.uuid4().hex[:12]
            url = f"{settings.auth.backend_base_url}/mock-bac-checkout/{order_id}?ref={ref}&amount={amount}"
            return PaymentLink(url=url, provider=PROVIDER_MOCK)

        base = settings.auth.backend_base_url.rstrip("/")
        token = order_webhook_token(order_id)
        number = order.get("request_number")
        description = f"Solicitud #{number}" if number else f"Pedido {order_id}"
        try:
            url, link_id = await tilopay_client.create_payment_link(
                creds,
                amount=amount,
                # Tilopay exige referencias únicas para siempre: el _id de Mongo lo es.
                reference=order_id,
                description=description,
                client_name=str(order.get("student_id") or ""),
                callback_url=f"{base}/webhooks/tilopay/return/{order_id}/{token}",
                webhook_url=f"{base}/webhooks/tilopay/{order_id}/{token}",
            )
        except TilopayError as exc:
            logger.error("No se pudo crear el link de Tilopay para %s: %s", order_id, exc)
            raise PaymentGatewayError("No se pudo generar el link de pago. Intenta aceptar de nuevo.") from exc
        return PaymentLink(url=url, provider=PROVIDER_TILOPAY, link_id=link_id, account_user=creds.api_user)

    async def _credentials_for(self, order: dict) -> TilopayCredentials:
        """Cuenta del restaurante del pedido, la misma donde se creó el link. Lanza TilopayError si ya no está."""
        try:
            creds = await self.accounts.get_credentials(order["restaurant_id"])
        except PaymentAccountError as exc:
            raise TilopayError(str(exc)) from exc
        if creds is None:
            raise TilopayError(f"El restaurante {order['restaurant_id']} ya no tiene cuenta de Tilopay configurada.")
        expected = order.get("payment_account_user")
        if expected and expected != creds.api_user:
            raise TilopayError(
                f"El link del pedido {order.get('id')} se creó en otra cuenta de Tilopay ({expected}); "
                "revisar el pago a mano en esa cuenta."
            )
        return creds

    async def is_paid(self, order: dict) -> bool:
        """Consulta a Tilopay si el link del pedido ya tiene un pago aprobado.

        Lanza TilopayError si no se pudo consultar (no es lo mismo que "no pagado").
        """
        if order.get("payment_provider") != PROVIDER_TILOPAY or not order.get("payment_link_id"):
            return False
        creds = await self._credentials_for(order)
        detail = await tilopay_client.get_payment_link_detail(creds, order["payment_link_id"])
        payments = detail.get("payments") or []
        return any(_payment_is_approved(p) for p in payments)

    async def discard_link(self, order: dict) -> None:
        """Borra el link de un pedido cancelado para que ya no se pueda pagar (mejor esfuerzo)."""
        if order.get("payment_provider") != PROVIDER_TILOPAY or not order.get("payment_link_id"):
            return
        try:
            creds = await self._credentials_for(order)
            await tilopay_client.delete_payment_link(creds, order["payment_link_id"])
        except TilopayError as exc:
            logger.warning("No se pudo borrar el link de Tilopay %s: %s", order["payment_link_id"], exc)
