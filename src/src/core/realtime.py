"""
Difusión en memoria de eventos de pedidos por WebSocket a los clientes
conectados a ESTA instancia del proceso.

Tres tipos de suscriptores:

- **Estación KDS** (`/ws/kds/{restaurant_id}` con token de KDS): recibe los
  eventos de las solicitudes/órdenes ofrecidas o asignadas a esa estación.
  Si la orden no tiene estación (restaurante sin estaciones configuradas),
  la reciben todas las estaciones del restaurante.
- **Observador de restaurante** (`/ws/kds/{restaurant_id}` con token de
  admin): recibe todos los eventos del restaurante en formato KDS.
- **Consola de administración** (`/ws/admin`): recibe el registro de
  eventos (`order_events`) filtrado por restaurante (o todos) y estación.

El estado de conexión de cada estación (`connected_since`, `last_seen`)
también vive aquí. Con más de una instancia del API, cada instancia conoce
solo sus propias conexiones.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timezone

from fastapi import WebSocket

logger = logging.getLogger("realtime")


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _dumps(payload: dict) -> str | None:
    try:
        return json.dumps(payload, default=_json_default, ensure_ascii=False)
    except Exception:
        logger.exception("No se pudo serializar un mensaje de WebSocket.")
        return None


class ConnectionManager:
    def __init__(self) -> None:
        self._kds: dict[str, set[WebSocket]] = defaultdict(set)
        self._kds_meta: dict[str, dict] = {}
        self._watchers: dict[str, set[WebSocket]] = defaultdict(set)
        self._admins: dict[WebSocket, dict] = {}
        self._lock = asyncio.Lock()

    # --- Registro -----------------------------------------------------------

    async def connect_kds(self, kds_user_id: str, restaurant_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        now = datetime.now(timezone.utc)
        async with self._lock:
            if not self._kds.get(kds_user_id):
                self._kds_meta[kds_user_id] = {"restaurant_id": restaurant_id, "connected_since": now, "last_seen": now}
            self._kds[kds_user_id].add(websocket)
            self._kds_meta[kds_user_id]["last_seen"] = now

    async def disconnect_kds(self, kds_user_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._kds[kds_user_id].discard(websocket)
            if not self._kds[kds_user_id]:
                self._kds.pop(kds_user_id, None)
                meta = self._kds_meta.get(kds_user_id)
                if meta is not None:
                    meta["connected_since"] = None

    def touch_kds(self, kds_user_id: str) -> None:
        meta = self._kds_meta.get(kds_user_id)
        if meta is not None:
            meta["last_seen"] = datetime.now(timezone.utc)

    async def connect_watcher(self, restaurant_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._watchers[restaurant_id].add(websocket)

    async def disconnect_watcher(self, restaurant_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._watchers[restaurant_id].discard(websocket)
            if not self._watchers[restaurant_id]:
                self._watchers.pop(restaurant_id, None)

    async def connect_admin(self, websocket: WebSocket, restaurant_id: str | None, kds_user_id: str | None) -> None:
        """`restaurant_id=None` = todos los restaurantes (solo superadmin)."""
        async with self._lock:
            self._admins[websocket] = {"restaurant_id": restaurant_id, "kds_user_id": kds_user_id}

    async def disconnect_admin(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._admins.pop(websocket, None)

    # --- Estado de estaciones -------------------------------------------------

    def kds_status(self, kds_user_id: str) -> dict:
        meta = self._kds_meta.get(kds_user_id) or {}
        return {
            "connected": bool(self._kds.get(kds_user_id)),
            "connected_since": meta.get("connected_since") if self._kds.get(kds_user_id) else None,
            "last_seen": meta.get("last_seen"),
        }

    def is_kds_connected(self, kds_user_id: str) -> bool:
        return bool(self._kds.get(kds_user_id))

    # --- Envío ----------------------------------------------------------------

    async def _send(self, websocket: WebSocket, message: str) -> bool:
        try:
            await websocket.send_text(message)
            return True
        except Exception:
            return False

    async def dispatch(self, payload: dict) -> None:
        """Difunde un evento de pedido. `payload` trae `restaurant_id`,
        `target_kds_ids` (lista; vacía = todas las estaciones del restaurante)
        y `event_record` (el documento de `order_events`). Nunca lanza."""
        restaurant_id = payload.get("restaurant_id")
        target_kds_ids = payload.get("target_kds_ids") or []
        record = payload.get("event_record")

        kds_message = _dumps({k: v for k, v in payload.items() if k not in ("event_record", "target_kds_ids")})
        if kds_message is not None and restaurant_id:
            if target_kds_ids:
                kds_ids = [k for k in target_kds_ids if k in self._kds]
            else:
                kds_ids = [k for k, meta in self._kds_meta.items() if meta.get("restaurant_id") == restaurant_id]
            for kds_id in kds_ids:
                for websocket in list(self._kds.get(kds_id, ())):
                    if not await self._send(websocket, kds_message):
                        await self.disconnect_kds(kds_id, websocket)
            for websocket in list(self._watchers.get(restaurant_id, ())):
                if not await self._send(websocket, kds_message):
                    await self.disconnect_watcher(restaurant_id, websocket)

        if record is None:
            return
        admin_message = _dumps({"type": "event", "event": record})
        if admin_message is None:
            return
        for websocket, filters in list(self._admins.items()):
            if filters["restaurant_id"] and filters["restaurant_id"] != record.get("restaurant_id"):
                continue
            if filters["kds_user_id"] and filters["kds_user_id"] not in (record.get("kds_user_ids") or []):
                continue
            if not await self._send(websocket, admin_message):
                await self.disconnect_admin(websocket)

    async def broadcast(self, restaurant_id: str | None, payload: dict) -> None:
        """Compatibilidad: difunde `payload` a todo el restaurante."""
        await self.dispatch({**payload, "restaurant_id": restaurant_id})


kds_manager = ConnectionManager()
