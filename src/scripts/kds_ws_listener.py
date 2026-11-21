"""
Cliente de prueba del canal en tiempo real del KDS (WebSocket), para ver los
eventos de pedidos tal como los recibiría la pantalla de cocina.

1. Hace login de KDS (POST /auth/kds/login) y toma el `restaurant_id` del
   usuario (o lo busca por nombre con --restaurant si el login no lo trae).
2. Carga la cola actual (GET /api/orders/active) como estado inicial.
3. Se conecta a /ws/kds/{restaurant_id}?token=... y muestra cada evento.

Correr en una terminal y dejarlo abierto; en OTRA terminal correr
scripts/simulate_order_flow.py (o usar Swagger / el bot) para ver llegar
order_created, order_accepted, order_paid, order_ready, order_delivered...

    uv run python scripts/kds_ws_listener.py                      # gh-uvg (Golden Harvest, todo menos almuerzos)
    uv run python scripts/kds_ws_listener.py --username gh-spazio  # Golden Harvest, almuerzos
    uv run python scripts/kds_ws_listener.py --username gitane1 --pin 1234
    uv run python scripts/kds_ws_listener.py --base-url https://<tu-api>.onrender.com

No importa `src`, se corre directo (sin -m). Ctrl+C para salir.
"""

import argparse
import asyncio
import json
from datetime import datetime

import httpx
import websockets

EVENT_ICONS = {
    "order_created": "🆕",
    "order_accepted": "👍",
    "order_paid": "💰",
    "order_ready": "✅",
    "order_rejected": "⛔",
    "order_payment_failed": "💸",
    "order_delivered": "📦",
    "order_offered": "📨",
    "order_rejected_by_station": "↪️",
    "payment_link_sent": "🔗",
    "order_in_preparation": "🍳",
    "payment_link_expired": "⌛",
    "order_cancelled": "🚫",
    "otp_failed": "🔒",
}


def _login(
    base_url: str, username: str, pin: str, restaurant_name: str | None, restaurant_id_override: str | None
) -> tuple[str, str]:
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        resp = client.post("/auth/kds/login", json={"username": username, "pin": pin})
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        restaurant_id = data.get("restaurant_id")
        print(f"Login KDS OK: {data.get('username')} @ {data.get('restaurant_name')} (restaurant_id={restaurant_id})")

        restaurants = client.get("/api/restaurants/", params={"active_only": "true"}).json()
        active_ids = {r["id"] for r in restaurants}
        if restaurant_id_override:
            restaurant_id = restaurant_id_override
            print(f"Usando --restaurant-id {restaurant_id} (ignora el del usuario de KDS).")
        elif not restaurant_id or restaurant_id not in active_ids:
            if restaurant_id:
                print(
                    f"(!) El restaurant_id del usuario de KDS ({restaurant_id}) NO es ningún restaurante activo -- "
                    "el pedido de la simulación nunca llegaría a ese canal. Corrige `restaurant_id` en la "
                    "colección kds_users. Por ahora se busca el restaurante por nombre."
                )
            name = restaurant_name or data.get("restaurant_name")
            matches = [r for r in restaurants if r["name"] == name]
            if not matches:
                raise SystemExit(f"No se encontró un restaurante activo llamado '{name}'.")
            if len(matches) > 1:
                print(f"(!) Hay {len(matches)} restaurantes activos llamados '{name}'; se usa el primero, "
                      "igual que simulate_order_flow.py.")
            restaurant_id = matches[0]["id"]
            print(f"Escuchando el restaurante '{name}' con id {restaurant_id}.")

        active_resp = client.get("/api/orders/active", headers={"Authorization": f"Bearer {token}"})
        active_resp.raise_for_status()
        mine = active_resp.json()
        print(f"Cola inicial (GET /api/orders/active): {len(mine)} pedido(s) de esta estación")
        for o in mine:
            number = f"Orden #{o['order_number']}" if o.get("order_number") else f"Solicitud #{o.get('request_number')}"
            print(f"   - {number}  {o['pipeline_status']}/{o['payment_status']}  Q{o['total_amount']}")
        return token, restaurant_id


def _print_event(payload: dict, raw: bool) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    event = payload.get("event")
    order = payload.get("order") or {}
    print(f"\n[{ts}] {EVENT_ICONS.get(event, '•')} {event}  pedido={order.get('id')}")
    if payload.get("message"):
        print(f"         {payload['message']}")
    print(f"         estado: {order.get('pipeline_status')} / pago: {order.get('payment_status')}")
    items = ", ".join(f"{i.get('product_name')} x{i.get('quantity')}" for i in order.get("items", []))
    if items:
        print(f"         items: {items}")
    if payload.get("preview_ready_at"):
        print(f"         preview_ready_at: {payload['preview_ready_at']}")
    if order.get("ready_at"):
        print(f"         ready_at: {order['ready_at']}  otp: {order.get('otp_code')}")
    if raw:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


async def listen(
    base_url: str, username: str, pin: str, restaurant_name: str | None, restaurant_id: str | None, raw: bool
) -> None:
    token, restaurant_id = _login(base_url, username, pin, restaurant_name, restaurant_id)
    ws_url = base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    ws_url = f"{ws_url}/ws/kds/{restaurant_id}?token={token}"

    backoff = 2
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20) as ws:
                print(f"\nConectado a /ws/kds/{restaurant_id} -- esperando eventos (Ctrl+C para salir)...")
                backoff = 2
                async def keepalive():
                    while True:
                        await asyncio.sleep(30)
                        await ws.send("ping")  # mantiene la estación como "conectada" en el panel

                pinger = asyncio.create_task(keepalive())
                try:
                    async for message in ws:
                        if message == "pong":
                            continue
                        _print_event(json.loads(message), raw)
                finally:
                    pinger.cancel()
        except websockets.ConnectionClosed as exc:
            if exc.rcvd and exc.rcvd.code == 1008:
                raise SystemExit(f"El servidor cerró con 1008 (token inválido/expirado o rol sin permiso): {exc.rcvd.reason}")
            print(f"Conexión cerrada ({exc}). Reintentando en {backoff}s...")
        except OSError as exc:
            print(f"No se pudo conectar ({exc}). Reintentando en {backoff}s...")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--username", default="gh-uvg", help="Usuario KDS (default: gh-uvg, cocina UVG de Golden Harvest)")
    parser.add_argument("--pin", default="1234")
    parser.add_argument("--restaurant", default=None, help="Nombre del restaurante, solo si el login no trae restaurant_id")
    parser.add_argument("--restaurant-id", default=None, help="Forzar el canal de este restaurant_id")
    parser.add_argument("--raw", action="store_true", help="Imprime además el JSON completo de cada evento")
    args = parser.parse_args()
    try:
        asyncio.run(listen(args.base_url, args.username, args.pin, args.restaurant, args.restaurant_id, args.raw))
    except KeyboardInterrupt:
        print("\nListener detenido.")
    except httpx.ConnectError:
        raise SystemExit(
            f"No se pudo conectar a la API en {args.base_url}. ¿Está corriendo? "
            "Levántala primero con `docker compose up` (o `uv run uvicorn main:app --reload`) "
            "y confirma que http://localhost:8000/docs abre en el navegador."
        )
    except httpx.HTTPStatusError as exc:
        raise SystemExit(
            f"El login de KDS falló ({exc.response.status_code}): {exc.response.text}. "
            "¿Existe el usuario? Créalo con `docker compose exec api python -m scripts.seed_kds_user`."
        )
