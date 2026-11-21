"""Texto de los avisos por WhatsApp del pedido (src/services/notification_templates.py)."""

from datetime import datetime, timezone

import pytest

from src.configuration import settings
from src.services import notification_templates as nt

ORDER = {
    "id": "665f1b2c8f1b2c8f1b2c8f1b",
    "request_number": 2563,
    "order_number": 3397,
    "otp_code": "5140",
    "total_amount": 43.0,
    "ready_at": datetime(2026, 10, 4, 23, 25, tzinfo=timezone.utc),  # 5:25 pm Guatemala
    "payment_expires_at": datetime(2026, 10, 4, 23, 20, tzinfo=timezone.utc),
    "items": [
        {"product_name": "Baguette chapin", "quantity": 1},
        {"product_name": "Pepsi lata 355ml", "quantity": 2},
    ],
}


# --- Los tres que ya no repiten el detalle del pedido -------------------------------------------


def test_pago_confirmado_sin_detalle_del_pedido():
    text = nt.order_paid_message(ORDER)
    assert "Pedido:" not in text and "Baguette" not in text
    assert "Pago confirmado. Tu orden es la #3397." in text
    assert "Hora estimada de entrega: 5:25 pm" in text
    assert "Código para recoger en mostrador: 5140" in text


def test_listo_para_recoger_sin_detalle_del_pedido():
    text = nt.order_ready_message(ORDER)
    assert "Pedido:" not in text and "Pepsi" not in text
    assert text == "Tu pedido ya está listo para recoger.\nCódigo para recoger en mostrador: 5140"


def test_listo_con_direccion_del_restaurante():
    text = nt.order_ready_message(
        {**ORDER, "restaurant_name": "Golden Harvest", "restaurant_location": "Edificio A, primer nivel"}
    )
    assert text == (
        "Tu pedido ya está listo para recoger en Golden Harvest.\n"
        "Dónde: Edificio A, primer nivel\n"
        "Código para recoger en mostrador: 5140"
    )


def test_entregado_con_link_de_opinion(monkeypatch):
    monkeypatch.setattr(settings.whatsapp, "rating_url", "https://forms.example.com/opinion")
    text = nt.order_delivered_message(ORDER)
    assert "Pedido:" not in text and "Baguette" not in text
    assert text.startswith("Pedido entregado. ¡Gracias por tu compra!")
    assert "https://forms.example.com/opinion" in text


def test_entregado_sin_link_solo_agradece(monkeypatch):
    monkeypatch.setattr(settings.whatsapp, "rating_url", "")
    assert nt.order_delivered_message(ORDER) == "Pedido entregado. ¡Gracias por tu compra!"


# --- Los que se quedan igual ---------------------------------------------------------------------


def test_aceptado_conserva_detalle_total_y_link():
    text = nt.order_accepted_message(ORDER, datetime(2026, 10, 4, 23, 16, tzinfo=timezone.utc), "https://pago.test/x")
    assert "Tu pedido #2563 fue aceptado." in text
    assert "Pedido: Baguette chapin, 2x Pepsi lata 355ml" in text
    assert "Hora estimada de entrega: 5:16 pm" in text
    assert "Total a pagar: Q43.00" in text
    assert "Paga antes de las 5:20 pm." in text
    assert "Link de pago: https://pago.test/x" in text


@pytest.mark.parametrize(
    "build",
    [nt.order_rejected_message, nt.order_payment_expired_message, nt.order_payment_failed_message],
)
def test_avisos_de_cancelacion_siguen_diciendo_que_pedido_era(build):
    assert "Pedido: Baguette chapin, 2x Pepsi lata 355ml" in build(ORDER)


# --- Plantillas de Meta ----------------------------------------------------------------------------


def test_plantilla_de_entregado_no_lleva_variables():
    template = nt.order_delivered_template(ORDER)
    assert template.name == settings.whatsapp.template_delivered
    assert template.body_params == []
    assert template.url_button_param is None  # el botón es estático en Meta


def test_plantilla_de_aceptado_mantiene_sus_cinco_variables():
    template = nt.order_accepted_template(ORDER, datetime(2026, 10, 4, 23, 16, tzinfo=timezone.utc))
    assert template.body_params == [
        "2563",
        "Baguette chapin, 2x Pepsi lata 355ml",
        "5:16 pm",
        "Q43.00",
        "5:20 pm",
    ]
