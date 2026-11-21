"""
Canales en tiempo real (WebSocket).

- `/ws/kds/{restaurant_id}?token=...`: pantalla de una estación KDS. Recibe un
  mensaje JSON por cada evento de las solicitudes/órdenes ofrecidas o
  asignadas a esa estación. El cliente manda `ping` periódicamente (cada
  30 s) y recibe `pong`; eso actualiza el estado de conexión de la estación.
  Con token de administración del restaurante se conecta como observador
  (recibe todos los eventos del restaurante en el mismo formato).
- `/ws/admin?token=...&restaurant_id=...&kds_user_id=...`: consola de
  eventos del panel de administración. Al conectar envía `backfill` con los
  eventos de hoy y después cada evento nuevo como `event`.

El token va en la query string porque el WebSocket del navegador no permite
enviar el header `Authorization`.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from src.api.v1.deps import AdminScope, get_admin_scope, load_kds_from_claims
from src.core.database import mongo
from src.core.realtime import _json_default, kds_manager
from src.core.security import decode_access_token
from src.core.timeutils import local_date_str
from src.models.admin_user import ADMIN_ROLES, LEGACY_ADMIN_ROLE
from src.repositories.order_event_repo import OrderEventRepository

logger = logging.getLogger("kds_stream")

router = APIRouter()


async def _reject(websocket: WebSocket, reason: str) -> None:
    await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason)


async def _claims_or_close(websocket: WebSocket, token: str | None) -> dict | None:
    if not token:
        await _reject(websocket, "Falta el parámetro 'token'.")
        return None
    try:
        return decode_access_token(token)
    except ValueError as exc:
        await _reject(websocket, str(exc))
        return None


async def _admin_scope_or_close(websocket: WebSocket, claims: dict) -> AdminScope | None:
    try:
        return await get_admin_scope(claims, mongo.db)
    except HTTPException as exc:
        await _reject(websocket, str(exc.detail))
        return None


@router.websocket("/kds/{restaurant_id}")
async def kds_stream(websocket: WebSocket, restaurant_id: str, token: str | None = None):
    claims = await _claims_or_close(websocket, token)
    if claims is None:
        return

    if claims.get("role") == "kds":
        try:
            kds_user = await load_kds_from_claims(claims, mongo.db)
        except HTTPException as exc:
            await _reject(websocket, str(exc.detail))
            return
        if kds_user.get("restaurant_id") and kds_user["restaurant_id"] != restaurant_id:
            await _reject(websocket, "Este KDS pertenece a otro restaurante.")
            return
        kds_id = kds_user["id"]
        await kds_manager.connect_kds(kds_id, restaurant_id, websocket)
        logger.info("KDS '%s' conectado al canal de '%s'.", kds_user["username"], restaurant_id)
        try:
            while True:
                message = await websocket.receive_text()
                kds_manager.touch_kds(kds_id)
                if message.strip().lower() in ("ping", '"ping"', '{"type":"ping"}', '{"type": "ping"}'):
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            pass
        finally:
            await kds_manager.disconnect_kds(kds_id, websocket)
            logger.info("KDS '%s' desconectado.", kds_user["username"])
        return

    if claims.get("role") in (*ADMIN_ROLES, LEGACY_ADMIN_ROLE):
        scope = await _admin_scope_or_close(websocket, claims)
        if scope is None:
            return
        if not scope.is_superadmin and scope.restaurant_id != restaurant_id:
            await _reject(websocket, "Solo puedes observar tu restaurante.")
            return
        await kds_manager.connect_watcher(restaurant_id, websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await kds_manager.disconnect_watcher(restaurant_id, websocket)
        return

    await _reject(websocket, "Rol sin permiso para este canal.")


@router.websocket("/admin")
async def admin_stream(
    websocket: WebSocket,
    token: str | None = None,
    restaurant_id: str | None = None,
    kds_user_id: str | None = None,
):
    claims = await _claims_or_close(websocket, token)
    if claims is None:
        return
    scope = await _admin_scope_or_close(websocket, claims)
    if scope is None:
        return
    try:
        target_restaurant = scope.resolve_restaurant(restaurant_id, allow_all=True)
    except HTTPException as exc:
        await _reject(websocket, str(exc.detail))
        return

    await websocket.accept()
    # Registrar antes del backfill: el front descarta eventos con `seq` repetido.
    await kds_manager.connect_admin(websocket, target_restaurant, kds_user_id or None)
    try:
        events = await OrderEventRepository(mongo.db).find_for_day(local_date_str(), target_restaurant, kds_user_id)
        await websocket.send_text(
            json.dumps(
                {"type": "backfill", "date": local_date_str(), "events": events},
                default=_json_default,
                ensure_ascii=False,
            )
        )
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() in ("ping", '"ping"', '{"type":"ping"}', '{"type": "ping"}'):
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await kds_manager.disconnect_admin(websocket)
