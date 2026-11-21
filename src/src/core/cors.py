"""
CORS para los frontends (kds, admin, ops y register).

Cada frontend se publica en su propio dominio (por ejemplo `https://qorder-admin.onrender.com`),
distinto al del API, así que el navegador bloquea sus peticiones salvo que el API responda con
las cabeceras CORS. Los orígenes permitidos se configuran con la variable `CORS_ORIGINS`. Sin ella, en producción no se agrega ninguna cabecera CORS (cerrado por defecto).

Las rutas WebSocket (`/ws/...`) no pasan por CORS: el navegador no lo aplica al handshake.
"""

import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Puertos de desarrollo de los frontends (kds 5173, admin 5174, register 5175, ops 5176).
# Solo se usan en DEVELOPMENT y sin CORS_ORIGINS; normalmente Vite los atiende con su proxy.
DEV_ORIGINS = [f"http://{host}:{port}" for port in (5173, 5174, 5175, 5176) for host in ("localhost", "127.0.0.1")]

# Solo estas cabeceras de petición y respuesta le hacen falta a los frontends.
ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = ["Authorization", "Content-Type", "Accept"]
# Sin esto el navegador no deja leer el nombre del archivo en las descargas de Excel.
EXPOSED_HEADERS = ["Content-Disposition"]
PREFLIGHT_MAX_AGE_SECONDS = 600


def parse_origins(raw: str | None) -> list[str]:
    """`"https://a.com, https://b.com/"` -> `["https://a.com", "https://b.com"]`.

    Acepta comas, espacios o saltos de línea como separador y quita la `/` final: el navegador
    manda el origen sin ella y una coincidencia exacta fallaría por ese detalle.
    """
    seen: dict[str, None] = {}
    for item in re.split(r"[,\s]+", raw or ""):
        origin = item.strip().rstrip("/")
        if origin:
            seen.setdefault(origin, None)
    return list(seen)


def setup_cors(app: FastAPI, origins: list[str], origin_regex: str = "") -> bool:
    """Agrega CORSMiddleware si hay orígenes (o regex) permitidos. Devuelve si quedó activo.

    `allow_credentials` queda en False a propósito: la sesión viaja en la cabecera
    `Authorization`, no en cookies, así que no hace falta (y es más seguro).
    """
    if not origins and not origin_regex:
        return False
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=origin_regex or None,
        allow_credentials=False,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSED_HEADERS,
        max_age=PREFLIGHT_MAX_AGE_SECONDS,
    )
    return True
