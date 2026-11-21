"""Fase 6: panel Dev/TI y restablecimiento de contraseña."""

import pytest

from src.integrations import email_client


@pytest.fixture()
def outbox(monkeypatch):
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(email_client, "send_password_reset_email", fake_send)
    return sent


def _token(mail):
    return mail["url"].split("token=")[1]


def test_create_restaurant_and_initial_password(client, seed, outbox):
    ti = seed["login_admin"]("ti@uvg.com")
    gh = seed["login_admin"]("admin@gh.com")
    body = {"restaurant_name": "Café Nuevo", "admin_username": "Ana", "admin_email": "Ana@CafeNuevo.com"}
    assert client.post("/api/ti/restaurants", json=body, headers=gh).status_code == 403
    r = client.post("/api/ti/restaurants", json=body, headers=ti)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["admin"]["password_pending"] is True and created["email_sent"] is True
    assert outbox[0]["welcome"] is True and outbox[0]["to"] == "ana@cafenuevo.com"
    assert client.post("/api/ti/restaurants", json=body, headers=ti).status_code == 409

    token = _token(outbox[0])
    info = client.get(f"/auth/password-reset/validate?token={token}").json()
    assert info["purpose"] == "welcome"
    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "nueva-clave-1"}).status_code == 204
    assert client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "otra-clave-2"}).status_code == 400

    login = client.post("/auth/admin/login", json={"email": "ana@cafenuevo.com", "password": "nueva-clave-1"}).json()
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    restaurant = client.get("/api/admin/restaurant", headers=headers).json()
    assert restaurant["opening_hours"]["sunday"]["closed"] is True
    assert restaurant["opening_hours"]["monday"] == {"closed": False, "open": "08:00", "close": "20:00"}
    menu = client.get("/api/admin/menu", headers=headers).json()
    assert menu[0]["code"] == "EJEMPLO-1" and menu[0]["available"] is False

    restaurants = {r["name"]: r for r in client.get("/api/ti/restaurants", headers=ti).json()}
    assert restaurants["Café Nuevo"]["admins"] == 1


def test_reset_existing_admin_and_listing(client, seed, outbox):
    ti = seed["login_admin"]("ti@uvg.com")
    users = client.get("/api/ti/restaurant-users", headers=ti).json()
    assert {u["email"] for u in users} == {"admin@gh.com", "admin@gitane.com"}
    gh_admin = next(u for u in users if u["email"] == "admin@gh.com")
    r = client.post(f"/api/ti/restaurant-users/{gh_admin['id']}/password-reset", headers=ti)
    assert r.status_code == 200 and r.json()["purpose"] == "reset"
    first = _token(outbox[-1])
    client.post(f"/api/ti/restaurant-users/{gh_admin['id']}/password-reset", headers=ti)
    assert client.get(f"/auth/password-reset/validate?token={first}").status_code == 400  # el enlace anterior se invalida

    assert client.post("/auth/password-reset/request", json={"email": "nadie@x.com"}).status_code == 202
    assert len(client.get("/api/ti/kds-users", headers=ti).json()) == 3
    assert client.get("/api/ti/events", headers=ti).json()["events"] == []


def test_create_admin_for_existing_restaurant(client, seed, outbox):
    ti = seed["login_admin"]("ti@uvg.com")
    body = {"restaurant_id": seed["gh_id"], "username": "Gerente GH", "email": "gerente@gh.com", "password": "clave-inicial"}
    r = client.post("/api/ti/restaurant-users", json=body, headers=ti)
    assert r.status_code == 201, r.text
    assert r.json()["admin"]["must_change_password"] is True and r.json()["email_sent"] is False
    login = client.post("/auth/admin/login", json={"email": "gerente@gh.com", "password": "clave-inicial"}).json()
    assert login["restaurant_id"] == seed["gh_id"] and login["must_change_password"] is True
    assert client.post("/api/ti/restaurant-users", json=body, headers=ti).status_code == 409

    r = client.post("/api/ti/restaurant-users", json={"restaurant_id": seed["gh_id"], "username": "Otro", "email": "otro@gh.com"}, headers=ti)
    assert r.json()["email_sent"] is True and outbox[-1]["welcome"] is True
    gh = seed["login_admin"]("admin@gh.com")
    assert client.post("/api/ti/restaurant-users", json=body, headers=gh).status_code == 403
