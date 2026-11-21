"""
Cliente HTTP crudo hacia Tilopay (links de pago).

Referencia: https://tilopay.com/en/developers (API · Links de pago / Autenticación).

- Cada restaurante tiene su propia cuenta de Tilopay: todas las funciones
  reciben las credenciales (`TilopayCredentials`) de la cuenta a usar. Ver
  src/services/payment_accounts.py para cómo se guardan.
- Un solo host para pruebas y producción (`https://app.tilopay.com`): el modo
  (test / producción) se cambia en el portal de Tilopay, no desde el código.
- `POST /api/v1/login` {apiuser, password} -> {access_token, expires_in}. El
  token dura 24 h; aquí se guarda en memoria por cuenta y se renueva solo.
- `POST /api/v1/createLinkPayment` -> {type: "200", url, id}.
- `GET  /api/v1/getDetailLinkPayment/{id}/{key}` -> {detail, payments: []}.
- `POST /api/v1/deleteLinkPayment` {id}.

Esta capa no sabe nada de pedidos: ver src/services/payment_service.py.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

import httpx

from src.configuration import settings

logger = logging.getLogger("tilopay")
# httpx registra la URL completa de cada request en INFO, y la de
# getDetailLinkPayment lleva la API key en la ruta.
logging.getLogger("httpx").setLevel(logging.WARNING)

_TOKEN_SAFETY_MARGIN_SECONDS = 300
# Token en memoria por cuenta: {cache_key: (token, expira_en_monotonic)}.
_tokens: dict[str, tuple[str, float]] = {}
_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class TilopayCredentials:
    """Credenciales de una cuenta de Tilopay (las de un restaurante)."""

    api_user: str
    api_password: str = field(repr=False)
    api_key: str = field(repr=False)
    currency: str = "GTQ"

    @property
    def cache_key(self) -> str:
        # Incluye la contraseña (hasheada) para que un cambio de credenciales no reuse el token viejo.
        digest = hashlib.sha256(f"{self.api_user}\0{self.api_password}".encode()).hexdigest()
        return f"{self.api_user}:{digest[:16]}"


class TilopayError(Exception):
    """Tilopay respondió con error, o no se pudo contactar."""


class TilopayAuthError(TilopayError):
    """Tilopay rechazó el usuario/contraseña de la API."""


def _client() -> httpx.AsyncClient:
    cfg = settings.tilopay
    return httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), timeout=cfg.timeout_seconds)


def reset_token_cache() -> None:
    _tokens.clear()
    _locks.clear()


async def _login(creds: TilopayCredentials) -> str:
    try:
        async with _client() as client:
            response = await client.post(
                "/api/v1/login", json={"apiuser": creds.api_user, "password": creds.api_password}
            )
    except httpx.HTTPError as exc:
        raise TilopayError(f"No se pudo contactar a Tilopay (login): {exc.__class__.__name__}") from exc
    data = _json(response)
    token = data.get("access_token")
    if response.status_code != 200 or not token:
        raise TilopayAuthError(f"Tilopay rechazó el usuario o la contraseña de la API (HTTP {response.status_code}).")
    expires_in = max(60, int(data.get("expires_in") or 86400) - _TOKEN_SAFETY_MARGIN_SECONDS)
    _tokens[creds.cache_key] = (token, time.monotonic() + expires_in)
    return token


async def _get_token(creds: TilopayCredentials, force: bool = False) -> str:
    lock = _locks.setdefault(creds.cache_key, asyncio.Lock())
    async with lock:
        cached = _tokens.get(creds.cache_key)
        if not force and cached and time.monotonic() < cached[1]:
            return cached[0]
        return await _login(creds)


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _request(
    creds: TilopayCredentials, method: str, path: str, *, json: dict | None = None, label: str
) -> dict:
    """Request autenticado; si el token ya no sirve (401), hace login de nuevo una vez."""
    for attempt in (0, 1):
        token = await _get_token(creds, force=attempt == 1)
        try:
            async with _client() as client:
                response = await client.request(method, path, json=json, headers={"Authorization": f"bearer {token}"})
        except httpx.HTTPError as exc:
            raise TilopayError(f"No se pudo contactar a Tilopay ({label}): {exc.__class__.__name__}") from exc
        if response.status_code == 401 and attempt == 0:
            continue
        data = _json(response)
        if response.status_code != 200 or str(data.get("type", "200")) != "200":
            raise TilopayError(
                f"Tilopay rechazó {label} (HTTP {response.status_code}, type={data.get('type')}): {data.get('message')}"
            )
        return data
    raise TilopayError(f"Tilopay rechazó {label}.")  # pragma: no cover


async def verify_credentials(creds: TilopayCredentials) -> None:
    """Hace login con las credenciales. Lanza TilopayAuthError si Tilopay las rechaza.

    Solo valida usuario y contraseña: Tilopay no tiene una forma de validar la API key sin crear un link.
    """
    await _get_token(creds, force=True)


async def create_payment_link(
    creds: TilopayCredentials,
    *,
    amount: float,
    reference: str,
    description: str,
    client_name: str = "",
    callback_url: str = "",
    webhook_url: str = "",
) -> tuple[str, str]:
    """Crea un link de un solo uso en la cuenta `creds`. Devuelve (url, id_del_link)."""
    data = await _request(
        creds,
        "POST",
        "/api/v1/createLinkPayment",
        json={
            "key": creds.api_key,
            "amount": f"{amount:.2f}",
            "currency": creds.currency,
            "reference": reference,
            "type": 1,  # 1 = un solo uso, 0 = ilimitado
            "description": description[:250],
            "client": client_name,
            "callback_url": callback_url,
            "webhook_url": webhook_url,
        },
        label="createLinkPayment",
    )
    url, link_id = data.get("url"), data.get("id")
    if not url or link_id is None:
        raise TilopayError("Tilopay no devolvió la URL o el id del link de pago.")
    return str(url), str(link_id)


async def get_payment_link_detail(creds: TilopayCredentials, link_id: str) -> dict:
    """Detalle del link y sus pagos asociados: {detail: {...}, payments: [...]}."""
    return await _request(
        creds, "GET", f"/api/v1/getDetailLinkPayment/{link_id}/{creds.api_key}", label="getDetailLinkPayment"
    )


async def delete_payment_link(creds: TilopayCredentials, link_id: str) -> None:
    await _request(creds, "POST", "/api/v1/deleteLinkPayment", json={"id": link_id}, label="deleteLinkPayment")
