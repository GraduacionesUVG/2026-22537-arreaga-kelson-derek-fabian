"""Fallback en proceso: sin Kafka, el API manda el WhatsApp al estudiante (ver src/integrations/events.py)."""

import asyncio
from datetime import datetime, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from src.configuration import settings
from src.core.database import mongo
from src.integrations import events as events_module
from src.integrations import whatsapp_client

ORDER = {
    "id": "6abc4c50f570dbdd19c72a57",
    "student_id": "22075",
    "restaurant_id": "rest-1",
    "items": [{"product_name": "Papas con queso", "quantity": 1}],
    "pipeline_status": "accepted",
    "payment_status": "pending",
    "total_amount": 32.0,
    "request_number": 2559,
    "payment_reference": "https://pago.test/abc",
    "payment_expires_at": datetime(2026, 9, 30, 0, 10, tzinfo=timezone.utc),
    "offered_kds_id": "kds-1",
    "assigned_kds_id": "kds-1",
}


@pytest.fixture()
def env(monkeypatch):
    db = AsyncMongoMockClient(tz_aware=True)["gp_notify_test"]
    mongo.db = db
    sent: list[tuple[str, str]] = []

    async def fake_send_text(to, text):
        sent.append((to, text))
        return {}

    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)
    monkeypatch.setattr(settings.kafka, "inline_notifications", True)
    publisher = events_module.EventPublisher()  # sin productor = Kafka apagado
    yield db, sent, publisher
    mongo.db = None


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _publish_and_wait(publisher, event, order, extra=None):
    await publisher.publish_order_event(event, order, extra=extra)
    if publisher._inline_tasks:
        await asyncio.wait(set(publisher._inline_tasks), timeout=5)


def test_aceptado_sin_kafka_manda_whatsapp_y_registra(env):
    db, sent, publisher = env

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(
            publisher, "order_accepted", dict(ORDER), extra={"preview_ready_at": "2026-09-30T00:20:00+00:00"}
        )
        return await db.order_events.find({"event": "customer_notified"}).to_list(None)

    notified = _run(scenario())
    assert len(sent) == 1
    to, text = sent[0]
    assert to == "50255550000"
    assert "#2559 fue aceptado" in text and "https://pago.test/abc" in text
    assert len(notified) == 1 and notified[0]["data"]["notification"] == "link de pago"


def test_order_created_no_notifica(env):
    db, sent, publisher = env

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_created", dict(ORDER, pipeline_status="pending_acceptance"))

    _run(scenario())
    assert sent == []


def test_estudiante_sin_usuario_no_falla(env):
    db, sent, publisher = env
    _run(_publish_and_wait(publisher, "order_ready", dict(ORDER)))
    assert sent == []


def test_envio_fallido_no_registra_customer_notified(env, monkeypatch):
    db, sent, publisher = env

    async def failing_send_text(to, text):
        raise whatsapp_client.WhatsAppApiError("token vencido")

    monkeypatch.setattr(whatsapp_client, "send_text", failing_send_text)

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_paid", dict(ORDER, payment_status="paid", order_number=2054))
        return await db.order_events.count_documents({"event": "customer_notified"})

    assert _run(scenario()) == 0


def test_flag_apagado_no_notifica(env, monkeypatch):
    db, sent, publisher = env
    monkeypatch.setattr(settings.kafka, "inline_notifications", False)

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_ready", dict(ORDER))

    _run(scenario())
    assert sent == []


# --- Message Templates -------------------------------------------------------


@pytest.fixture()
def templates(env, monkeypatch):
    db, sent, publisher = env
    calls: list[dict] = []

    async def fake_send_message(to, payload):
        calls.append({"to": to, **payload})
        return {}

    monkeypatch.setattr(whatsapp_client, "send_message", fake_send_message)
    monkeypatch.setattr(settings.whatsapp, "templates_enabled", True)
    monkeypatch.setattr(settings.whatsapp, "payment_url_base", "https://pago.test/")
    return db, sent, publisher, calls


def test_aceptado_con_templates_manda_boton_dinamico(templates):
    db, sent, publisher, calls = templates

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(
            publisher, "order_accepted", dict(ORDER), extra={"preview_ready_at": "2026-09-30T00:20:00+00:00"}
        )

    _run(scenario())
    assert sent == []  # nada de texto con el link pegado
    template = calls[0]["template"]
    assert calls[0]["type"] == "template" and template["name"] == "link_de_pago"
    body, button = template["components"]
    assert body["parameters"][0]["text"] == "2559" and body["parameters"][3]["text"] == "Q32.00"
    assert button["sub_type"] == "url" and button["parameters"][0]["text"] == "abc"  # lo que sigue a PAYMENT_URL_BASE
    assert all("\n" not in p["text"] for p in body["parameters"])


def test_entregado_manda_template_con_boton_estatico(templates):
    db, sent, publisher, calls = templates

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_delivered", dict(ORDER, pipeline_status="delivered"))
        return await db.order_events.find({"event": "customer_notified"}).to_list(None)

    notified = _run(scenario())
    template = calls[0]["template"]
    assert template["name"] == "pedido_entregado"
    assert "components" not in template  # cuerpo sin variables y botón estático: nada que enviar
    assert notified[0]["data"]["notification"] == "pedido entregado"


def test_template_rechazada_cae_a_texto(templates, monkeypatch):
    db, sent, publisher, calls = templates

    async def rejecting_send_message(to, payload):
        if payload["type"] == "template":
            raise whatsapp_client.WhatsAppApiError("template no aprobada")
        return {}

    monkeypatch.setattr(whatsapp_client, "send_message", rejecting_send_message)

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_accepted", dict(ORDER))

    _run(scenario())
    assert len(sent) == 1 and "https://pago.test/abc" in sent[0][1]


def test_templates_apagadas_o_evento_sin_boton_salen_como_texto(templates, monkeypatch):
    db, sent, publisher, calls = templates

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_ready", dict(ORDER))  # sin template: texto
        monkeypatch.setattr(settings.whatsapp, "templates_enabled", False)
        await _publish_and_wait(publisher, "order_delivered", dict(ORDER))  # apagadas: texto

    _run(scenario())
    assert calls == [] and len(sent) == 2
    assert "listo para recoger" in sent[0][1] and "entregado" in sent[1][1]


def test_listo_incluye_la_direccion_del_restaurante(env):
    db, sent, publisher = env

    async def scenario():
        restaurant = await db.restaurants.insert_one({"name": "Golden Harvest", "location": "Edificio A, primer nivel"})
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_ready", dict(ORDER, restaurant_id=str(restaurant.inserted_id)))

    _run(scenario())
    text = sent[0][1]
    assert "listo para recoger en Golden Harvest" in text and "Dónde: Edificio A, primer nivel" in text


def test_listo_sin_restaurante_valido_sale_sin_direccion(env):
    db, sent, publisher = env

    async def scenario():
        await db.users.insert_one({"student_id": "22075", "whatsapp_number": "50255550000"})
        await _publish_and_wait(publisher, "order_ready", dict(ORDER))  # restaurant_id "rest-1": no existe

    _run(scenario())
    assert sent[0][1].startswith("Tu pedido ya está listo para recoger.") and "Dónde" not in sent[0][1]
