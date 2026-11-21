"""
Publicación de eventos de pedidos hacia dos destinos independientes,
ninguno de los cuales puede impedir que la operación principal (crear/
aceptar/pagar/entregar un pedido) se complete si falla.

1. Difusión en memoria a los clientes de KDS conectados por WebSocket a
   ESTA misma instancia del proceso (src/core/realtime.py). Es lo único que
   hace falta para un despliegue de una sola instancia del API (el caso de
   este proyecto hoy): el pedido nuevo aparece en el KDS sin que nadie tenga
   que hacer polling.
2. Publicación a un tópico de Kafka (ORDERS_TOPIC), solo si
   `settings.kafka.enabled=True`. Cubre dos casos que la difusión en memoria
   por sí sola no resuelve:
   - Más de una instancia del API corriendo a la vez: cada instancia relee
     el tópico y reenvía a sus propios clientes de WebSocket conectados
     (ver relay_events_to_kds), así que un pedido aceptado en la instancia A
     también le llega al KDS conectado a la instancia B.
   - Desacoplar el envío de WhatsApp (llamada HTTP externa, lenta y que
     puede fallar) del ciclo de request/response principal: un proceso
     aparte (src/worker.py) consume el mismo tópico y manda los mensajes,
     sin bloquear ni arriesgar la respuesta al KDS/BAC que disparó el evento.

Con Kafka apagado o caído, el punto 1 sigue funcionando solo -- por eso
Kafka es la pieza opcional del diseño, no un requisito para operar.

3. Fallback en proceso del aviso por WhatsApp: si el evento NO llegó a
   Kafka (apagado, sin conexión, o falló el envío de ese evento) y es uno
   de los que le importan al estudiante, se manda el WhatsApp desde este
   mismo proceso en una tarea de fondo (`asyncio.create_task`), con la
   misma lógica que usa el worker (src/services/customer_notifications.py).
   La respuesta al KDS/BAC no espera ese envío. Se apaga con
   NOTIFY_INLINE_WITHOUT_KAFKA=false.
"""

import asyncio

import json
import logging
import uuid
from datetime import datetime

from src.configuration import settings
from src.core.realtime import kds_manager

logger = logging.getLogger("events")

ORDERS_TOPIC = "orders.events"

# Identifica a este proceso como origen de un evento: se usa para que, si
# esta misma instancia también consume el tópico (relay_events_to_kds), no
# vuelva a difundir por WebSocket un evento que ya difundió al publicarlo.
INSTANCE_ID = uuid.uuid4().hex


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class EventPublisher:
    """Un solo punto de publicación para toda la app (`event_publisher`, al
    final de este archivo) -- evita que cada llamador tenga que acordarse de
    hacer los dos pasos (WebSocket + Kafka) y el try/except de cada uno."""

    def __init__(self) -> None:
        self._producer = None  # aiokafka.AIOKafkaProducer, se crea en start()
        # Tareas de aviso por WhatsApp en proceso (fallback sin Kafka). Se
        # guarda la referencia para que el GC no las cancele a medio camino
        # y para poder esperarlas al apagar la app.
        self._inline_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Llamado desde el lifespan de main.py. Si Kafka está apagado o no
        se puede conectar, queda en modo degradado (self._producer=None) y
        publish_order_event() simplemente no intenta usar Kafka -- no tumba
        el arranque de la API."""
        if not settings.kafka.enabled:
            logger.info("Kafka deshabilitado (KAFKA_ENABLED=false) -- solo difusión WebSocket en memoria.")
            return
        try:
            from aiokafka import AIOKafkaProducer

            self._producer = AIOKafkaProducer(bootstrap_servers=settings.kafka.bootstrap_servers)
            await self._producer.start()
            logger.info("Productor de Kafka conectado (%s).", settings.kafka.bootstrap_servers)
        except Exception:
            logger.exception(
                "No se pudo conectar el productor de Kafka (%s) -- se sigue operando solo con "
                "difusión WebSocket en memoria.",
                settings.kafka.bootstrap_servers,
            )
            self._producer = None

    async def stop(self) -> None:
        if self._inline_tasks:
            # Dar unos segundos a los WhatsApp en vuelo antes de apagar.
            await asyncio.wait(set(self._inline_tasks), timeout=5)
        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception:
                logger.exception("Error cerrando el productor de Kafka (se ignora, la app ya está cerrando).")

    async def publish_order_event(
        self,
        event_type: str,
        order: dict,
        extra: dict | None = None,
        actor: dict | None = None,
        target_kds_ids: list[str] | None = None,
    ) -> dict | None:
        """Punto único de publicación de un evento de pedido.

        1. Guarda el evento en `order_events` (con número de secuencia y texto legible).
        2. Lo difunde por WebSocket (estaciones KDS, observadores y consola de admin).
        3. Lo publica en Kafka si está habilitado.

        Nunca lanza: un error aquí no debe tumbar la operación que lo disparó.
        Devuelve el registro guardado (o `None` si no se pudo guardar).
        """
        extra = extra or {}
        record = None
        try:
            record = await record_order_event(event_type, order, extra=extra, actor=actor)
        except Exception:
            logger.exception("Error guardando el evento '%s' en order_events.", event_type)

        if target_kds_ids is None:
            target_kds_ids = default_target_kds_ids(order)

        payload = {
            "event": event_type,
            "order": order,
            "restaurant_id": order.get("restaurant_id"),
            "instance_id": INSTANCE_ID,
            "message": record.get("message") if record else None,
            "seq": record.get("seq") if record else None,
            **extra,
            "target_kds_ids": target_kds_ids,
            "event_record": record,
        }

        try:
            await kds_manager.dispatch(payload)
        except Exception:
            logger.exception("Error difundiendo el evento '%s' por WebSocket.", event_type)

        sent_to_kafka = False
        if self._producer is not None:
            try:
                body = json.dumps(payload, default=_json_default).encode("utf-8")
                await self._producer.send_and_wait(ORDERS_TOPIC, body)
                sent_to_kafka = True
            except Exception:
                logger.exception("Error publicando el evento '%s' en Kafka.", event_type)

        if not sent_to_kafka:
            # Nadie (el worker) va a ver este evento: el aviso sale de aquí.
            self._notify_customer_inline(payload)
        return record

    async def publish_station_status(self, station: dict) -> None:
        """Avisa a TODAS las pantallas abiertas de una estación (y a los observadores del
        restaurante) que la estación se pausó o reanudó: `{"type": "station_status", ...}`.
        Va por WebSocket y por Kafka (si está habilitado) para llegar a las demás instancias.
        Nunca lanza."""
        payload = {
            "type": "station_status",
            "event": "station_status",
            "restaurant_id": station.get("restaurant_id"),
            "kds_user_id": station.get("id") or station.get("kds_user_id"),
            "username": station.get("username"),
            "active": bool(station.get("active", True)),
            "instance_id": INSTANCE_ID,
            "target_kds_ids": [station.get("id") or station.get("kds_user_id")],
            "event_record": None,
        }
        try:
            await kds_manager.dispatch(payload)
        except Exception:
            logger.exception("Error difundiendo el estado de la estación por WebSocket.")
        if self._producer is not None:
            try:
                await self._producer.send_and_wait(ORDERS_TOPIC, json.dumps(payload, default=_json_default).encode("utf-8"))
            except Exception:
                logger.exception("Error publicando el estado de la estación en Kafka.")

    async def publish_shift_status(self, shift: dict) -> None:
        """Avisa a todas las pantallas de la estación (y a los observadores del restaurante)
        que se inició o cerró su turno: `{"type": "shift_status", "status": "open"|"closed", "shift": {...}}`.
        Nunca lanza."""
        payload = {
            "type": "shift_status",
            "event": "shift_status",
            "restaurant_id": shift.get("restaurant_id"),
            "kds_user_id": shift.get("kds_user_id"),
            "status": shift.get("status"),
            "shift": shift,
            "instance_id": INSTANCE_ID,
            "target_kds_ids": [shift.get("kds_user_id")],
            "event_record": None,
        }
        try:
            await kds_manager.dispatch(payload)
        except Exception:
            logger.exception("Error difundiendo el estado del turno por WebSocket.")
        if self._producer is not None:
            try:
                await self._producer.send_and_wait(ORDERS_TOPIC, json.dumps(payload, default=_json_default).encode("utf-8"))
            except Exception:
                logger.exception("Error publicando el estado del turno en Kafka.")

    def _notify_customer_inline(self, payload: dict) -> None:
        """Programa el WhatsApp al estudiante en segundo plano (punto 3 del docstring del módulo)."""
        if not settings.kafka.inline_notifications:
            return
        from src.services.customer_notifications import should_notify

        if not should_notify(payload.get("event")):
            return
        try:
            task = asyncio.create_task(self._run_inline_notification(payload))
        except RuntimeError:
            logger.warning("Sin event loop activo: no se envía el WhatsApp de '%s'.", payload.get("event"))
            return
        self._inline_tasks.add(task)
        task.add_done_callback(self._inline_tasks.discard)

    async def _run_inline_notification(self, payload: dict) -> None:
        """Nunca lanza: un aviso fallido solo se registra en el log."""
        try:
            from src.core.database import mongo
            from src.repositories.user_repo import UserRepository
            from src.services.customer_notifications import notify_customer
            from src.services.whatsapp_service import WhatsAppService

            if mongo.db is None:
                return
            record = await notify_customer(payload, UserRepository(mongo.db), WhatsAppService())
            if record is None:
                return
            order = payload.get("order") or {}
            logger.info(
                "Aviso por WhatsApp en proceso enviado: '%s' del pedido %s.", payload.get("event"), order.get("id")
            )
            await kds_manager.dispatch(
                {
                    "event": "customer_notified",
                    "order": order,
                    "restaurant_id": order.get("restaurant_id"),
                    "instance_id": INSTANCE_ID,
                    "message": record.get("message"),
                    "seq": record.get("seq"),
                    "target_kds_ids": [],
                    "event_record": record,
                }
            )
        except Exception:
            logger.exception("Error en el aviso por WhatsApp en proceso del evento '%s'.", payload.get("event"))


def default_target_kds_ids(order: dict) -> list[str]:
    """Estación(es) que deben recibir el evento en su pantalla. Lista vacía = todas."""
    if order.get("assigned_kds_id"):
        return [order["assigned_kds_id"]]
    if order.get("offered_kds_id"):
        return [order["offered_kds_id"]]
    return []


def _event_kds_ids(order: dict, actor: dict | None) -> list[str]:
    ids = [order.get("offered_kds_id"), order.get("assigned_kds_id"), *(order.get("declined_kds_ids") or [])]
    if actor and actor.get("type") == "kds":
        ids.append(actor.get("id"))
    seen: list[str] = []
    for kds_id in ids:
        if kds_id and kds_id not in seen:
            seen.append(kds_id)
    return seen


async def record_order_event(
    event_type: str, order: dict, extra: dict | None = None, actor: dict | None = None, db=None
) -> dict | None:
    """Inserta el evento en `order_events` y devuelve el documento serializado."""
    from src.core.database import mongo
    from src.core.timeutils import local_date_str, utc_now
    from src.models.order_event import COLLECTION_NAME, describe_event
    from src.repositories.counter_repo import CounterRepository

    db = db if db is not None else mongo.db
    if db is None:
        return None
    extra = extra or {}
    now = utc_now()
    data = {k: v for k, v in extra.items() if isinstance(v, (str, int, float, bool)) or v is None}
    data.setdefault("total_amount", order.get("total_amount"))
    document = {
        "seq": await CounterRepository(db).next("event_seq"),
        "event": event_type,
        "restaurant_id": order.get("restaurant_id"),
        "kds_user_ids": _event_kds_ids(order, actor),
        "order_id": order.get("id"),
        "request_number": order.get("request_number"),
        "order_number": order.get("order_number"),
        "pipeline_status": order.get("pipeline_status"),
        "payment_status": order.get("payment_status"),
        "actor": actor or {"type": "system", "id": None, "username": None},
        "message": describe_event(event_type, order, extra, actor),
        "data": data,
        "created_at": now,
        "local_date": local_date_str(now),
    }
    result = await db[COLLECTION_NAME].insert_one(document)
    document["id"] = str(result.inserted_id)
    document.pop("_id", None)
    return document


event_publisher = EventPublisher()


async def relay_events_to_kds() -> None:
    """Tarea de fondo (arrancada desde el lifespan de main.py cuando Kafka
    está habilitado): consume ORDERS_TOPIC y reenvía cada evento a los
    clientes de KDS conectados por WebSocket a ESTA instancia.

    Solo hace falta cuando hay más de una instancia del API corriendo a la
    vez -- la instancia que publicó el evento ya lo difundió directo
    (EventPublisher.publish_order_event), así que acá se descarta con
    `instance_id` para no duplicar el mensaje en esa misma instancia.
    `group_id` único por proceso (no un valor fijo) para que cada instancia
    reciba TODOS los eventos -- acá se quiere que cada instancia vea todo
    (fan-out), no que se repartan los mensajes entre instancias como un
    consumer group compartido haría (eso sería balanceo de carga, que es lo
    que sí se quiere en src/worker.py).
    """
    if not settings.kafka.enabled:
        return

    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        ORDERS_TOPIC,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=f"kds-relay-{INSTANCE_ID}",
        auto_offset_reset="latest",
    )
    try:
        await consumer.start()
    except Exception:
        logger.exception(
            "No se pudo conectar el consumidor de reenvío a KDS (%s) -- se sigue operando solo con "
            "difusión WebSocket en memoria de esta instancia.",
            settings.kafka.bootstrap_servers,
        )
        return

    try:
        async for message in consumer:
            try:
                payload = json.loads(message.value.decode("utf-8"))
                if payload.get("instance_id") == INSTANCE_ID:
                    continue  # esta misma instancia ya lo difundió al publicarlo
                await kds_manager.dispatch(payload)
            except Exception:
                logger.exception("Error reenviando un evento de Kafka al KDS.")
    finally:
        await consumer.stop()
