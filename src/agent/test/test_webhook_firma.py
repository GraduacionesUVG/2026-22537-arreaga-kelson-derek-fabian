"""
Firma X-Hub-Signature-256 de Meta en el webhook: sin firma válida el POST se rechaza (403)
y no se procesa nada.

    pytest agent/test/test_webhook_firma.py -v
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import webhook

SECRET = "app-secret-de-prueba"
BODY = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {"metadata": {}}}]}]}).encode()


def _firma(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture()
def procesados(monkeypatch):
    monkeypatch.setattr(webhook, "WHATSAPP_APP_SECRET", SECRET)
    vistos = []
    monkeypatch.setattr(webhook, "_handle_messages_change", lambda value: vistos.append(value))
    return vistos


def _post(headers):
    return TestClient(webhook.app).post("/", content=BODY, headers={"Content-Type": "application/json", **headers})


def test_firma_valida_se_procesa(procesados):
    assert _post({"X-Hub-Signature-256": _firma(BODY)}).status_code == 200
    assert len(procesados) == 1


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Hub-Signature-256": "sha256=deadbeef"}, {"X-Hub-Signature-256": _firma(BODY, "otro-secreto")}],
)
def test_sin_firma_o_firma_invalida_da_403(procesados, headers):
    assert _post(headers).status_code == 403
    assert procesados == []


def test_sin_app_secret_no_se_verifica(procesados, monkeypatch):
    monkeypatch.setattr(webhook, "WHATSAPP_APP_SECRET", "")
    assert _post({}).status_code == 200
