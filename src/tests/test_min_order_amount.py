"""Pedido mínimo (`MIN_ORDER_AMOUNT`, Q25.00 por defecto): se valida sobre la suma de productos,
sin la tarifa de servicio, en POST /api/orders/estimate y POST /api/orders/."""

import pytest

from src.configuration import settings

PAPAS = [{"product_name": "Papas", "quantity": 1}]  # Gitane, Q15
DOS_PAPAS = [{"product_name": "Papas", "quantity": 2}]  # Q30


@pytest.fixture(autouse=True)
def minimo_25(monkeypatch):
    monkeypatch.setattr(settings.payment, "min_order_amount", 25.0)


def _body(seed, items):
    return {"student_id": "20231", "restaurant_id": seed["gi_id"], "items": items}


@pytest.mark.parametrize("path", ["/api/orders/estimate", "/api/orders/"])
def test_pedido_bajo_el_minimo_se_rechaza_con_mensaje_para_el_bot(client, seed, path):
    r = client.post(path, json=_body(seed, PAPAS))
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "below_minimum_order"
    assert (detail["minimum_amount"], detail["subtotal_amount"], detail["missing_amount"]) == (25.0, 15.0, 10.0)
    assert "El pedido tiene que ser de mínimo Q25.00" in detail["message"]
    assert "te faltan Q10.00" in detail["message"]


def test_no_se_crea_la_solicitud(client, seed, db):
    from tests.conftest import run

    assert client.post("/api/orders/", json=_body(seed, PAPAS)).status_code == 400
    assert run(db["orders"].count_documents({})) == 0


def test_pedido_que_llega_al_minimo_pasa(client, seed):
    assert client.post("/api/orders/estimate", json=_body(seed, DOS_PAPAS)).status_code == 200
    r = client.post("/api/orders/", json=_body(seed, DOS_PAPAS))
    assert r.status_code == 201, r.text
    assert r.json()["subtotal_amount"] == 30.0


def test_exactamente_el_minimo_pasa(client, seed, monkeypatch):
    monkeypatch.setattr(settings.payment, "min_order_amount", 15.0)
    assert client.post("/api/orders/", json=_body(seed, PAPAS)).status_code == 201


def test_la_tarifa_de_servicio_no_cuenta_para_el_minimo(client, seed):
    headers = seed["login_admin"]("ti@uvg.com")
    r = client.put(
        "/api/admin/restaurant/service-fee", json={"service_fee": 12}, headers=headers,
        params={"restaurant_id": seed["gi_id"]},
    )
    assert r.status_code == 200, r.text
    # Q15 de productos + Q12 de tarifa = Q27, pero los productos no llegan a Q25.
    r = client.post("/api/orders/estimate", json=_body(seed, PAPAS))
    assert r.status_code == 400
    assert r.json()["detail"]["subtotal_amount"] == 15.0


def test_minimo_cero_desactiva_la_regla(client, seed, monkeypatch):
    monkeypatch.setattr(settings.payment, "min_order_amount", 0)
    assert client.post("/api/orders/", json=_body(seed, PAPAS)).status_code == 201
