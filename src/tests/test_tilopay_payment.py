"""Pagos con links de Tilopay, con una cuenta por restaurante (cliente HTTP simulado)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.configuration import settings
from src.integrations import tilopay_client
from src.integrations.tilopay_client import TilopayAuthError, TilopayError
from src.services.payment_service import order_webhook_token

GH_ACCOUNT = {"api_user": "gh-api", "api_password": "gh-pass", "api_key": "8080-1630-9939-3269-3133", "currency": "GTQ"}


@pytest.fixture()
def tilopay(monkeypatch):
    """Reemplaza las llamadas HTTP a Tilopay y registra con qué cuenta se hizo cada una."""
    state = {
        "created": [], "deleted": [], "payments": [], "logins": [],
        "fail_create": False, "fail_detail": False, "reject_login": False,
    }

    async def verify(creds):
        state["logins"].append(creds)
        if state["reject_login"]:
            raise TilopayAuthError("rechazado")

    async def create(creds, **kwargs):
        if state["fail_create"]:
            raise TilopayError("caído")
        state["created"].append({"creds": creds, **kwargs})
        return "https://app.tilopay.com/link/abc", "77"

    async def detail(creds, link_id):
        if state["fail_detail"]:
            raise TilopayError("caído")
        state.setdefault("detail_accounts", []).append(creds.api_user)
        return {"type": "200", "detail": {"id": int(link_id)}, "payments": state["payments"]}

    async def delete(creds, link_id):
        state["deleted"].append((creds.api_user, link_id))

    monkeypatch.setattr(tilopay_client, "verify_credentials", verify)
    monkeypatch.setattr(tilopay_client, "create_payment_link", create)
    monkeypatch.setattr(tilopay_client, "get_payment_link_detail", detail)
    monkeypatch.setattr(tilopay_client, "delete_payment_link", delete)
    return state


@pytest.fixture()
def gh_account(client, seed, tilopay):
    """Golden Harvest configura su cuenta de Tilopay desde el panel."""
    r = client.put("/api/admin/restaurant/payment-gateway", json=GH_ACCOUNT, headers=seed["login_admin"]("admin@gh.com"))
    assert r.status_code == 200, r.text
    return r.json()


def _accepted_order(client, seed, kds="gh-uvg", restaurant="gh_id", product="Café"):
    kds_headers, _ = seed["login_kds"](kds)
    order = client.post(
        "/api/orders/",
        json={"student_id": "20231", "restaurant_id": seed[restaurant], "items": [{"product_name": product, "quantity": 1}]},
    ).json()
    r = client.put(f"/api/orders/{order['id']}/status", json={"pipeline_status": "accepted"}, headers=kds_headers)
    return order, r


def _doc(db, **filter_):
    return asyncio.new_event_loop().run_until_complete(db.orders.find_one(filter_))


def _restaurant(db, restaurant_id):
    from bson import ObjectId

    return asyncio.new_event_loop().run_until_complete(db.restaurants.find_one({"_id": ObjectId(restaurant_id)}))


# --- Configuración de la cuenta ---------------------------------------------------------------


def test_admin_configures_account_and_secrets_are_encrypted(client, seed, db, tilopay, gh_account):
    assert gh_account == {
        "provider": "tilopay", "configured": True, "api_user": "gh-api", "api_key_last4": "3133",
        "currency": "GTQ", "verified_at": gh_account["verified_at"], "updated_at": gh_account["updated_at"],
    }
    assert tilopay["logins"][0].api_user == "gh-api"

    stored = _restaurant(db, seed["gh_id"])["payment_gateway"]
    assert "gh-pass" not in str(stored) and "8080-1630-9939-3269-3133" not in str(stored)
    assert stored["updated_by"]["username"] == "Admin GH"

    headers = seed["login_admin"]("admin@gh.com")
    got = client.get("/api/admin/restaurant/payment-gateway", headers=headers).json()
    assert got["configured"] is True and "api_password" not in got and "api_key" not in got
    assert client.get("/api/admin/restaurant", headers=headers).json()["payment_gateway_configured"] is True

    # Otro restaurante sigue sin cuenta, y un admin no puede tocar la de otro restaurante
    gi_headers = seed["login_admin"]("admin@gitane.com")
    assert client.get("/api/admin/restaurant/payment-gateway", headers=gi_headers).json()["configured"] is False
    r = client.put(
        f"/api/admin/restaurant/payment-gateway?restaurant_id={seed['gh_id']}", json=GH_ACCOUNT, headers=gi_headers
    )
    assert r.status_code == 403

    # Superadmin la ve en el listado de TI
    ti = client.get("/api/ti/restaurants", headers=seed["login_admin"]("ti@uvg.com")).json()
    assert {r["name"]: r["payment_gateway_configured"] for r in ti} == {"Golden Harvest": True, "Gitane": False}

    assert client.delete("/api/admin/restaurant/payment-gateway", headers=headers).status_code == 204
    assert client.get("/api/admin/restaurant/payment-gateway", headers=headers).json()["configured"] is False


def test_rejected_credentials_are_not_saved(client, seed, db, tilopay):
    tilopay["reject_login"] = True
    headers = seed["login_admin"]("admin@gh.com")
    r = client.put("/api/admin/restaurant/payment-gateway", json=GH_ACCOUNT, headers=headers)
    assert r.status_code == 422
    assert not _restaurant(db, seed["gh_id"]).get("payment_gateway")


def test_production_requires_credentials_secret(client, seed, db, tilopay, monkeypatch):
    monkeypatch.setattr(settings, "environment", "PRODUCTION")
    monkeypatch.setattr(settings.tilopay, "credentials_secret", "")
    r = client.put("/api/admin/restaurant/payment-gateway", json=GH_ACCOUNT, headers=seed["login_admin"]("admin@gh.com"))
    assert r.status_code == 503
    assert not _restaurant(db, seed["gh_id"]).get("payment_gateway")


# --- Cobro con la cuenta del restaurante ----------------------------------------------------------


def test_accept_creates_link_in_restaurant_account_and_webhook_confirms(client, seed, db, tilopay, gh_account):
    order, r = _accepted_order(client, seed)
    assert r.status_code == 200, r.text
    assert r.json()["payment_reference"] == "https://app.tilopay.com/link/abc"
    sent = tilopay["created"][0]
    assert sent["creds"].api_user == "gh-api" and sent["creds"].api_key == GH_ACCOUNT["api_key"]
    assert sent["reference"] == order["id"]
    token = order_webhook_token(order["id"])
    assert sent["webhook_url"].endswith(f"/webhooks/tilopay/{order['id']}/{token}")
    doc = _doc(db)
    assert (doc["payment_provider"], doc["payment_link_id"], doc["payment_account_user"]) == ("tilopay", "77", "gh-api")

    # El pago simulado no aplica a un pedido de Tilopay, y el token se valida
    assert client.post("/webhooks/bac-payment", json={"order_id": order["id"]}).status_code == 404
    assert client.post(f"/webhooks/tilopay/{order['id']}/malo", json={"code": "1"}).status_code == 403

    # Un webhook que dice "aprobado" no basta si Tilopay no lista el pago
    assert client.post(f"/webhooks/tilopay/{order['id']}/{token}", json={"code": "1"}).json() == {"result": "not_paid"}
    tilopay["payments"] = [{"code": "3"}]  # rechazado
    assert client.post(f"/webhooks/tilopay/{order['id']}/{token}", json={}).json() == {"result": "not_paid"}
    assert _doc(db)["pipeline_status"] == "accepted"

    tilopay["payments"] = [{"code": "3"}, {"code": "1"}]
    assert client.post(f"/webhooks/tilopay/{order['id']}/{token}", json={}).json() == {"result": "paid"}
    doc = _doc(db)
    assert doc["payment_status"] == "paid" and doc["order_number"] == 2001
    assert set(tilopay["detail_accounts"]) == {"gh-api"}
    r = client.get(f"/webhooks/tilopay/return/{order['id']}/{token}?code=1")
    assert r.status_code == 200 and "Pago recibido" in r.text
    assert _doc(db)["order_number"] == 2001


def test_restaurant_without_account_uses_simulated_payment_outside_production(client, seed, db, tilopay, gh_account):
    # Gitane no configuró Tilopay: en desarrollo se simula el pago, sin tocar la cuenta de Golden Harvest
    order, r = _accepted_order(client, seed, kds="gitane1", restaurant="gi_id", product="Papas")
    assert r.status_code == 200, r.text
    assert tilopay["created"] == []
    assert _doc(db)["payment_provider"] == "mock"
    r = client.post("/webhooks/bac-payment", json={"order_id": order["id"], "status": "success"})
    assert r.status_code == 200, r.text
    assert _doc(db)["payment_status"] == "paid"


def test_production_without_account_cannot_accept(client, seed, db, tilopay, monkeypatch):
    monkeypatch.setattr(settings, "environment", "PRODUCTION")
    order, r = _accepted_order(client, seed)
    assert r.status_code == 409
    assert "Tilopay" in r.json()["detail"]
    assert _doc(db)["pipeline_status"] == "pending_acceptance"
    # Y el pago simulado está apagado en producción
    assert client.post("/webhooks/bac-payment", json={"order_id": order["id"]}).status_code == 404


def test_simulated_payments_can_be_enabled_explicitly_in_production(client, seed, db, tilopay, monkeypatch):
    monkeypatch.setattr(settings, "environment", "PRODUCTION")
    monkeypatch.setattr(settings.tilopay, "simulated_payments_enabled", True)
    order, r = _accepted_order(client, seed)
    assert r.status_code == 200 and _doc(db)["payment_provider"] == "mock"


def test_gateway_down_keeps_request_pending(client, seed, db, tilopay, gh_account):
    tilopay["fail_create"] = True
    _, r = _accepted_order(client, seed)
    assert r.status_code == 502
    assert _doc(db)["pipeline_status"] == "pending_acceptance"


def test_expiry_reconciles_with_tilopay(client, seed, db, tilopay, gh_account):
    from src.services.payment_expiry import build_order_service

    order, _ = _accepted_order(client, seed)
    loop = asyncio.new_event_loop()
    service = build_order_service(db)
    soon = datetime.now(timezone.utc) + timedelta(minutes=16)

    # Tilopay caído: no se cancela todavía; pasado el margen, sí
    tilopay["fail_detail"] = True
    assert loop.run_until_complete(service.expire_unpaid_orders(soon)) == 0
    assert _doc(db)["pipeline_status"] == "accepted"

    # Pagó pero el webhook nunca llegó: se marca pagado en vez de cancelar
    tilopay["fail_detail"] = False
    tilopay["payments"] = [{"code": "1"}]
    assert loop.run_until_complete(service.expire_unpaid_orders(soon)) == 0
    assert _doc(db)["payment_status"] == "paid"
    assert tilopay["deleted"] == []


def test_expired_unpaid_deletes_link_and_flags_late_payment(client, seed, db, tilopay, gh_account):
    from src.services.payment_expiry import build_order_service

    order, _ = _accepted_order(client, seed)
    later = datetime.now(timezone.utc) + timedelta(minutes=20)
    assert asyncio.new_event_loop().run_until_complete(build_order_service(db).expire_unpaid_orders(later)) == 1
    assert tilopay["deleted"] == [("gh-api", "77")]
    assert _doc(db)["cancel_reason"] == "payment_link_expired"

    tilopay["payments"] = [{"code": "1"}]
    token = order_webhook_token(order["id"])
    assert client.post(f"/webhooks/tilopay/{order['id']}/{token}", json={}).json() == {"result": "late"}
    doc = _doc(db)
    assert doc["pipeline_status"] == "cancelled" and doc["late_payment_at"] is not None


def test_account_changed_after_link_is_not_queried_with_new_account(client, seed, db, tilopay, gh_account):
    order, _ = _accepted_order(client, seed)
    headers = seed["login_admin"]("admin@gh.com")
    other = {**GH_ACCOUNT, "api_user": "otra-cuenta"}
    assert client.put("/api/admin/restaurant/payment-gateway", json=other, headers=headers).status_code == 200

    tilopay["payments"] = [{"code": "1"}]
    token = order_webhook_token(order["id"])
    # No se consulta la cuenta nueva: el link vive en la anterior y se revisa a mano
    assert client.post(f"/webhooks/tilopay/{order['id']}/{token}", json={}).json() == {"result": "error"}
    assert "detail_accounts" not in tilopay


# --- Cliente HTTP: token y key por cuenta ---------------------------------------------------------


def test_client_keeps_one_token_per_account(monkeypatch):
    import json

    import httpx

    from src.integrations.tilopay_client import TilopayCredentials

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append((request.url.path, request.headers.get("authorization"), body))
        if request.url.path == "/api/v1/login":
            if body["password"] == "mala":
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json={"access_token": f"tok-{body['apiuser']}", "expires_in": 86400})
        return httpx.Response(200, json={"type": "200", "url": "https://pay/x", "id": 5})

    monkeypatch.setattr(
        tilopay_client, "_client", lambda: httpx.AsyncClient(base_url="https://app.tilopay.com", transport=httpx.MockTransport(handler))
    )
    tilopay_client.reset_token_cache()
    a = TilopayCredentials("cuenta-a", "pa", "key-a", "GTQ")
    b = TilopayCredentials("cuenta-b", "pb", "key-b", "USD")

    async def scenario():
        await tilopay_client.create_payment_link(a, amount=10, reference="r1", description="d")
        await tilopay_client.create_payment_link(b, amount=12.5, reference="r2", description="d")
        await tilopay_client.create_payment_link(a, amount=10, reference="r3", description="d")
        with pytest.raises(TilopayAuthError):
            await tilopay_client.verify_credentials(TilopayCredentials("cuenta-a", "mala", "key-a"))

    asyncio.new_event_loop().run_until_complete(scenario())
    logins = [c for c in calls if c[0] == "/api/v1/login"]
    links = [c for c in calls if c[0] == "/api/v1/createLinkPayment"]
    assert [c[2]["apiuser"] for c in logins] == ["cuenta-a", "cuenta-b", "cuenta-a"]  # el 3er login es el rechazado
    assert [(c[1], c[2]["key"], c[2]["currency"]) for c in links] == [
        ("bearer tok-cuenta-a", "key-a", "GTQ"),
        ("bearer tok-cuenta-b", "key-b", "USD"),
        ("bearer tok-cuenta-a", "key-a", "GTQ"),
    ]
    tilopay_client.reset_token_cache()
