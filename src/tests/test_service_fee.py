"""Tarifa de servicio por restaurante (`restaurants.service_fee`): se suma al total de cada pedido."""

from datetime import datetime, timezone

import pytest

from src.models.restaurant import service_fee_for
from src.schemas.order import OrderResponse
from src.services import notification_templates as nt

ITEMS = [{"product_name": "Hamburguesa", "quantity": 2, "modifiers": ["Doble queso"]}]  # (25 + 5) * 2 = 60


def _set_fee(client, headers, fee, restaurant_id=None):
    params = {"restaurant_id": restaurant_id} if restaurant_id else None
    return client.put("/api/admin/restaurant/service-fee", json={"service_fee": fee}, headers=headers, params=params)


def _order(client, restaurant_id, items=ITEMS):
    return client.post("/api/orders/", json={"student_id": "20231", "restaurant_id": restaurant_id, "items": items})


def test_service_fee_for_defaults_and_rounding():
    assert service_fee_for({}) == 0.0
    assert service_fee_for(None) == 0.0
    assert service_fee_for({"service_fee": None}) == 0.0
    assert service_fee_for({"service_fee": -3}) == 0.0
    assert service_fee_for({"service_fee": "2.65"}) == 2.65
    assert service_fee_for({"service_fee": 2.654}) == 2.65


def test_admin_sets_fee_and_it_is_added_to_estimate_and_order(client, seed):
    headers = seed["login_admin"]("admin@gh.com")
    assert client.get("/api/admin/restaurant/service-fee", headers=headers).json() == {"service_fee": 0.0}

    r = _set_fee(client, headers, 2.65)
    assert r.status_code == 200, r.text
    assert r.json()["service_fee"] == 2.65
    assert client.get("/api/admin/restaurant", headers=headers).json()["service_fee"] == 2.65

    r = client.post("/api/orders/estimate", json={"restaurant_id": seed["gh_id"], "items": ITEMS})
    assert r.status_code == 200, r.text
    est = r.json()
    assert (est["subtotal_amount"], est["service_fee"], est["total_amount"]) == (60.0, 2.65, 62.65)

    r = _order(client, seed["gh_id"])
    assert r.status_code == 201, r.text
    order = r.json()
    assert (order["subtotal_amount"], order["service_fee"], order["total_amount"]) == (60.0, 2.65, 62.65)

    # La lista pública de restaurantes también la expone (para que el bot avise antes de pedir).
    public = {x["id"]: x for x in client.get("/api/restaurants/").json()}
    assert public[seed["gh_id"]]["service_fee"] == 2.65


def test_fee_is_snapshot_and_charged_in_payment_link(client, seed):
    headers = seed["login_admin"]("admin@gh.com")
    _set_fee(client, headers, 2.65)
    order = _order(client, seed["gh_id"]).json()

    # Cambiar la tarifa no altera pedidos ya creados.
    _set_fee(client, headers, 5)
    kds_headers, _ = seed["login_kds"]("gh-uvg")
    r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=kds_headers)
    assert r.status_code == 200, r.text
    accepted = r.json()
    assert accepted["total_amount"] == 62.65
    assert accepted["service_fee"] == 2.65
    assert "amount=62.65" in accepted["payment_reference"]

    assert _order(client, seed["gh_id"]).json()["total_amount"] == 65.0


def test_fee_is_per_restaurant(client, seed):
    _set_fee(client, seed["login_admin"]("admin@gh.com"), 2.65)
    order = _order(client, seed["gi_id"], [{"product_name": "Papas", "quantity": 1}]).json()
    assert (order["subtotal_amount"], order["service_fee"], order["total_amount"]) == (15.0, 0.0, 15.0)


def test_fee_validation_and_scope(client, seed):
    headers = seed["login_admin"]("admin@gh.com")
    assert _set_fee(client, headers, -1).status_code == 422
    assert _set_fee(client, headers, 5000).status_code == 422
    assert _set_fee(client, headers, 2.654).json()["service_fee"] == 2.65  # se redondea a 2 decimales
    assert client.put("/api/admin/restaurant/service-fee", json={"service_fee": 1}).status_code in (401, 403)

    # Un admin de restaurante no puede cambiar la de otro; el superadmin sí, con ?restaurant_id=.
    ti = seed["login_admin"]("ti@uvg.com")
    r = _set_fee(client, ti, 1.5, restaurant_id=seed["gi_id"])
    assert r.status_code == 200, r.text
    assert r.json()["service_fee"] == 1.5


def test_legacy_order_without_subtotal():
    """Pedidos creados antes de la tarifa: sin `subtotal_amount` ni `service_fee` guardados."""
    now = datetime.now(timezone.utc)
    body = OrderResponse.model_validate({
        "id": "x", "student_id": "20231", "restaurant_id": "r", "items": [],
        "pipeline_status": "delivered", "payment_status": "paid", "total_amount": 40.0,
        "created_at": now, "updated_at": now,
    })
    assert (body.subtotal_amount, body.service_fee, body.total_amount) == (40.0, 0.0, 40.0)


def test_accepted_message_shows_breakdown_only_with_fee():
    base = {"request_number": 1, "items": [{"product_name": "Café", "quantity": 1}], "total_amount": 10.0}
    text = nt.order_accepted_message(base, None, "https://pago.test/x")
    assert "Total a pagar: Q10.00" in text and "Tarifa de servicio" not in text

    with_fee = base | {"subtotal_amount": 10.0, "service_fee": 2.65, "total_amount": 12.65}
    text = nt.order_accepted_message(with_fee, None, "https://pago.test/x")
    assert "Subtotal: Q10.00\nTarifa de servicio: Q2.65\nTotal a pagar: Q12.65" in text


def test_price_breakdown_text_in_estimate_and_order(client, seed):
    body = {"restaurant_id": seed["gh_id"], "items": ITEMS}
    assert client.post("/api/orders/estimate", json=body).json()["price_breakdown"] == "Total Q60.00"

    _set_fee(client, seed["login_admin"]("admin@gh.com"), 2.65)
    expected = "Subtotal Q60.00 + Tarifa de servicio Q2.65 = Total Q62.65"
    assert client.post("/api/orders/estimate", json=body).json()["price_breakdown"] == expected
    assert _order(client, seed["gh_id"]).json()["price_breakdown"] == expected


def test_agent_guardrail_accepts_fee_line_from_estimate(client, seed):
    """Con el texto del desglose en la respuesta de estimate, el guardrail del bot no confunde
    "Tarifa de servicio: Q2.65" con un producto inventado."""
    import json

    guardrails = pytest.importorskip("agent.guardrails")
    _set_fee(client, seed["login_admin"]("admin@gh.com"), 2.65)
    estimate = client.post("/api/orders/estimate", json={"restaurant_id": seed["gh_id"], "items": ITEMS}).json()
    menu = client.get("/api/menus/", params={"restaurant_id": seed["gh_id"]}).json()
    messages = [
        {"role": "user", "content": "quiero 2 hamburguesas con doble queso"},
        {"role": "assistant", "tool_calls": [{"id": "c0", "function": {"name": "ver_menu"}},
                                             {"id": "c1", "function": {"name": "estimar_pedido"}}]},
        {"role": "tool", "tool_call_id": "c0", "content": json.dumps({"items": menu})},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(estimate)},
    ]
    reply = "Tu pedido en Golden Harvest:\n- Hamburguesa: Q60.00\nTarifa de servicio: Q2.65\nTotal: Q62.65\n¿Lo confirmo?"
    assert guardrails.check_reply(reply, guardrails.collect_evidence(messages, 0)) == []

    # Sin el texto del desglose (solo números), la línea de la tarifa parecería un producto inventado.
    messages[-1]["content"] = json.dumps({k: v for k, v in estimate.items() if k != "price_breakdown"})
    violations = guardrails.check_reply(reply, guardrails.collect_evidence(messages, 0))
    assert any("Tarifa de servicio" in v for v in violations)
