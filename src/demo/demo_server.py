"""
Demo Runner del backend de QOrder -- servidor local para grabar la demo.

Sirve la página `demo/static/index.html` y hace de proxy hacia el backend
(producción por defecto). El proxy existe por tres razones:

1. CORS: el backend de producción solo acepta llamadas de navegador desde los
   frontends registrados en CORS_ORIGINS. El navegador habla con este servidor
   (localhost) y este servidor habla con el backend, así que no hace falta
   tocar la configuración de producción.
2. X-Internal-Api-Key: las rutas que en la vida real llama el bot de WhatsApp
   (`/api/restaurants/`, `/api/menus/`, `/api/orders/estimate`, `POST /api/orders/`,
   `GET /api/orders/{id}/status`, `/auth/whatsapp/check`) exigen la llave interna.
   La página nunca la ve: marca esas llamadas con `X-Demo-Internal: 1` y el
   proxy agrega la llave real.
3. WebSockets: `/ws-proxy/...` reenvía los canales `/ws/kds/{restaurant_id}` y
   `/ws/admin` para que todo pase por la misma dirección local.

Uso (desde la raíz del repo):

    uv run python demo/demo_server.py
    uv run python demo/demo_server.py --target http://localhost:8000      # contra el backend local
    INTERNAL_API_KEY=... uv run python demo/demo_server.py                # sin pregunta interactiva

Luego abrir http://localhost:8765 (se abre solo).
"""

import argparse
import asyncio
import getpass
import os
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

HERE = Path(__file__).resolve().parent
INDEX = HERE / "static" / "index.html"

DEFAULT_TARGET = "https://gp-agent-backend.onrender.com"

# Headers del navegador que sí se reenvían al backend. No se reenvía Origin/Cookie:
# para el backend, la llamada viene de un servicio, no de un navegador.
FORWARD_REQUEST_HEADERS = {"authorization", "content-type", "accept"}
FORWARD_RESPONSE_HEADERS = {"content-type", "content-disposition", "retry-after"}


def _ws_base(target: str) -> str:
    return target.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")


def build_app(target: str, internal_key: str | None, defaults: dict) -> FastAPI:
    target = target.rstrip("/")
    ws_base = _ws_base(target)
    state: dict = {"client": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Render (plan starter/free) puede tardar en despertar: timeout generoso.
        state["client"] = httpx.AsyncClient(base_url=target, timeout=httpx.Timeout(90.0, connect=30.0))
        yield
        await state["client"].aclose()

    app = FastAPI(title="QOrder Demo Runner", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(INDEX, headers={"Cache-Control": "no-store"})

    @app.get("/demo-config")
    async def demo_config() -> dict:
        return {
            "target": target,
            "internal_key_configured": bool(internal_key),
            "defaults": defaults,
        }

    @app.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request) -> Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        if request.headers.get("x-demo-internal") == "1" and internal_key:
            headers["X-Internal-Api-Key"] = internal_key
        body = await request.body()
        started = time.perf_counter()
        try:
            upstream = await state["client"].request(
                request.method,
                "/" + path,
                params=list(request.query_params.multi_items()),
                content=body or None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"detail": f"El proxy no pudo llegar al backend ({type(exc).__name__}: {exc})", "proxy_error": True},
                status_code=502,
                headers={"X-Demo-Upstream-Ms": str(round((time.perf_counter() - started) * 1000))},
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS}
        out_headers["X-Demo-Upstream-Ms"] = str(elapsed_ms)
        out_headers["Cache-Control"] = "no-store"
        return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)

    @app.websocket("/ws-proxy/{path:path}")
    async def ws_proxy(websocket: WebSocket, path: str) -> None:
        import websockets  # viene con uvicorn[standard]

        query = websocket.url.query
        url = f"{ws_base}/{path}" + (f"?{query}" if query else "")
        await websocket.accept()
        try:
            upstream = await websockets.connect(url, open_timeout=60, ping_interval=20, max_size=None)
        except Exception as exc:  # handshake rechazado (token inválido, 403...) o red
            reason = f"Backend rechazó la conexión: {exc}"[:120]
            await websocket.close(code=1008, reason=reason)
            return

        async def client_to_upstream() -> None:
            try:
                while True:
                    message = await websocket.receive_text()
                    await upstream.send(message)
            except Exception:  # el navegador cerró la pestaña o el backend cerró
                return

        async def upstream_to_client() -> None:
            try:
                async for message in upstream:
                    await websocket.send_text(
                        message if isinstance(message, str) else message.decode("utf-8", "replace")
                    )
            except Exception:
                return

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            close_code = getattr(upstream, "close_code", None)
            close_reason = getattr(upstream, "close_reason", None) or ""
            await upstream.close()
            try:
                await websocket.close(code=close_code if close_code and close_code >= 1000 else 1000,
                                      reason=str(close_reason)[:120])
            except (RuntimeError, WebSocketDisconnect):
                pass

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", default=os.getenv("DEMO_TARGET", DEFAULT_TARGET),
                        help=f"URL del backend (default: {DEFAULT_TARGET}).")
    parser.add_argument("--port", type=int, default=int(os.getenv("DEMO_PORT", "8765")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--restaurant-id", default=os.getenv("DEMO_RESTAURANT_ID", ""),
                        help="Opcional: si no se pasa, se usa el restaurante del admin al iniciar sesión.")
    parser.add_argument("--admin-email", default=os.getenv("DEMO_ADMIN_EMAIL", ""))
    parser.add_argument("--kds-username", default=os.getenv("DEMO_KDS_USERNAME", ""))
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador automáticamente.")
    parser.add_argument("--no-prompt", action="store_true",
                        help="No preguntar la llave interna si INTERNAL_API_KEY no está definida.")
    args = parser.parse_args()

    internal_key = os.getenv("DEMO_INTERNAL_API_KEY") or os.getenv("INTERNAL_API_KEY")
    if not internal_key and not args.no_prompt:
        print("Las rutas del bot (menú, estimar, crear pedido) exigen X-Internal-Api-Key.")
        internal_key = getpass.getpass("INTERNAL_API_KEY del backend (Enter para omitir): ").strip() or None

    defaults = {
        "restaurant_id": args.restaurant_id,
        "admin_email": args.admin_email,
        "kds_username": args.kds_username,
    }
    url = f"http://{args.host}:{args.port}"
    print(f"\n  QOrder Demo Runner -> {url}")
    print(f"  Backend: {args.target}")
    print(f"  Llave interna: {'configurada' if internal_key else 'NO configurada (las rutas del bot responderán 401/503)'}\n")
    if not args.no_browser:
        threading_timer_open(url)
    uvicorn.run(build_app(args.target, internal_key, defaults), host=args.host, port=args.port, log_level="warning")


def threading_timer_open(url: str) -> None:
    import threading

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()


if __name__ == "__main__":
    main()
