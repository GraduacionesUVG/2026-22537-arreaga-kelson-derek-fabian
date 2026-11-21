"""
Proceso aparte (`python -m src.worker`, o el servicio `worker` de
docker-compose.yml) que consume el tópico de eventos de pedidos y manda el
mensaje de WhatsApp correspondiente a cada evento.

Corre separado del proceso de la API a propósito: una llamada a la Graph
API de Meta puede tardar o fallar, y no debe compartir el mismo proceso (ni
arriesgar el mismo tiempo de respuesta) que crear/aceptar/pagar un pedido.
Con KAFKA_ENABLED=false (despliegue actual) este proceso no hace falta: el
API manda los mismos WhatsApp en una tarea de fondo (fallback en proceso,
ver src/integrations/events.py). Con Kafka encendido, el API ya no los
manda y este worker tiene que estar corriendo, o nadie avisa al estudiante.
"""

import asyncio
import json
import logging

from src.configuration import settings
from src.core.database import mongo
from src.repositories.user_repo import UserRepository
from src.services.customer_notifications import notify_customer
from src.services.whatsapp_service import WhatsAppService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

ORDERS_TOPIC = "orders.events"


async def _handle_event(
    payload: dict, user_repo: UserRepository, whatsapp_service: WhatsAppService, producer=None
) -> None:
    """Plantillas, búsqueda del número y registro `customer_notified` viven en
    src/services/customer_notifications.py (compartido con el fallback en
    proceso de EventPublisher cuando Kafka está apagado)."""
    record = await notify_customer(payload, user_repo, whatsapp_service)
    if record is not None:
        await _forward_notification(record, payload.get("order") or {}, producer)


async def _forward_notification(record: dict, order: dict, producer) -> None:
    """Reenvía `customer_notified` por Kafka para que el API lo difunda en vivo."""
    if producer is None:
        return
    payload = {
        "event": "customer_notified",
        "order": order,
        "restaurant_id": order.get("restaurant_id"),
        "instance_id": "worker",
        "message": record.get("message"),
        "seq": record.get("seq"),
        "target_kds_ids": [],
        "event_record": record,
    }
    try:
        await producer.send_and_wait(ORDERS_TOPIC, json.dumps(payload, default=str).encode("utf-8"))
    except Exception:
        logger.exception("No se pudo reenviar customer_notified por Kafka.")


async def run() -> None:
    if not settings.kafka.enabled:
        logger.warning("KAFKA_ENABLED=false -- este worker no tiene nada que consumir, se detiene.")
        return

    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    mongo.connect()
    user_repo = UserRepository(mongo.db)
    whatsapp_service = WhatsAppService()

    consumer = AIOKafkaConsumer(
        ORDERS_TOPIC,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        # Grupo fijo y compartido a propósito (a diferencia del relay hacia
        # el KDS, que usa un group_id único por instancia): si algún día hay
        # más de una réplica de este worker, Kafka reparte los eventos entre
        # ellas (balanceo de carga), en vez de que cada una mande el mismo
        # mensaje de WhatsApp por duplicado.
        group_id="whatsapp-notifier",
        auto_offset_reset="latest",
    )
    await consumer.start()
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka.bootstrap_servers)
    try:
        await producer.start()
    except Exception:
        logger.exception("No se pudo iniciar el productor de Kafka del worker; customer_notified solo se guardará.")
        producer = None
    logger.info(
        "Worker de WhatsApp conectado a Kafka (%s), escuchando '%s'.",
        settings.kafka.bootstrap_servers,
        ORDERS_TOPIC,
    )
    try:
        async for message in consumer:
            try:
                payload = json.loads(message.value.decode("utf-8"))
                await _handle_event(payload, user_repo, whatsapp_service, producer)
            except Exception:
                logger.exception("Error procesando un evento de Kafka, se continúa con el siguiente.")
    finally:
        await consumer.stop()
        if producer is not None:
            await producer.stop()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(run())
