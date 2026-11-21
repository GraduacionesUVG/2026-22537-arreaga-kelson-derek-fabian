"""Headers de seguridad en todas las respuestas HTTP del API.

Middleware ASGI puro (no toca WebSockets ni el cuerpo de las respuestas):

- `X-Content-Type-Options: nosniff`: el navegador no "adivina" el tipo de un archivo.
- `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`: el API no se puede meter en un
  iframe (clickjacking).
- `Referrer-Policy: no-referrer`: la página de retorno de Tilopay lleva un token en la URL;
  así no se filtra a otros sitios.
- `Permissions-Policy`: sin cámara, micrófono ni ubicación.
- `Cross-Origin-Opener-Policy: same-origin`.
- `Content-Security-Policy`: en respuestas JSON, `default-src 'none'` (nada se ejecuta).
  En HTML solo `frame-ancestors 'none'`, para no romper /docs ni la página de retorno.
- `Cache-Control: no-store` si la ruta no definió uno: tokens y datos de pedidos no quedan
  en cachés intermedias.
- `Strict-Transport-Security` solo en PRODUCTION (siempre detrás de HTTPS en Render).

Un header que la ruta ya haya puesto no se reemplaza.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_BASE_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "cross-origin-opener-policy": "same-origin",
    "cache-control": "no-store",
}
_CSP_API = "default-src 'none'; frame-ancestors 'none'"
_CSP_HTML = "frame-ancestors 'none'"
_HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _BASE_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
                if "content-security-policy" not in headers:
                    is_html = headers.get("content-type", "").startswith("text/html")
                    headers["content-security-policy"] = _CSP_HTML if is_html else _CSP_API
                if self.hsts and "strict-transport-security" not in headers:
                    headers["strict-transport-security"] = _HSTS
            await send(message)

        await self.app(scope, receive, send_with_headers)
