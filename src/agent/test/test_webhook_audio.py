"""
Tests del flujo de audio en agent/webhook.py (I-01 + I-02).

Se manda al webhook un payload con el formato real de Meta (nota de voz) y se
mockea todo lo externo: la descarga del audio (httpx.get -> bytes de un .ogg
de agent/test/audios), el backend (check_whatsapp_registration), el
orquestador (get_session) y el envío por WhatsApp.

- Unitarios: Whisper mockeado.
- Integración: Whisper real sobre el .ogg "descargado" (requiere OPENAI_API_KEY).

Correr desde la raíz del repo:
    pytest agent/test/test_webhook_audio.py -v -s
"""

import copy
import os
from collections import OrderedDict
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError

import webhook
from conftest import AUDIOS_DIR, HAS_REAL_OPENAI_KEY

AUDIOS = sorted(AUDIOS_DIR.glob("*.ogg"))

WA_ID = "50239058318"
AUDIO_URL = (
    "https://lookaside.fbsbx.com/whatsapp_business/attachments/"
    "?mid=29165689213021848&source=webhook&ext=1786930865&hash=ATyA26lW4i3nImqEqSYSZgEd"
)

# Formato real de Meta para una nota de voz entrante.
AUDIO_PAYLOAD = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "856664894044018",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15556611308",
                            "phone_number_id": "1282377331620289",
                        },
                        "contacts": [
                            {
                                "profile": {"name": "Diego Duarte"},
                                "wa_id": WA_ID,
                                "user_id": "GT.1347121187085726",
                            }
                        ],
                        "messages": [
                            {
                                "from": WA_ID,
                                "from_user_id": "GT.1347121187085726",
                                "id": "wamid.HBgLNTAyMzkwNTgzMTgVAgASGBQzQUMzNDgyREQyMDZFODgyRUZEOAA=",
                                "timestamp": "1786930564",
                                "type": "audio",
                                "audio": {
                                    "mime_type": "audio/ogg; codecs=opus",
                                    "sha256": "LGXuhP2R7WoCKjLYwJXx2tYct4Mi0eJFeAy2vtlkPpM=",
                                    "id": "29165689213021848",
                                    "url": AUDIO_URL,
                                    "voice": True,
                                },
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}


def _payload(audio_overrides: dict | None = None) -> dict:
    payload = copy.deepcopy(AUDIO_PAYLOAD)
    audio = payload["entry"][0]["changes"][0]["value"]["messages"][0]["audio"]
    audio.update(audio_overrides or {})
    return payload


@pytest.fixture
def client():
    return TestClient(webhook.app, raise_server_exceptions=False)


@pytest.fixture
def mocks(monkeypatch):
    """Mockea todo lo externo al webhook y devuelve los mocks para asserts."""
    audio_bytes = b"OggS fake audio"

    # Todos los tests usan el mismo wamid: sin esto, el segundo test lo vería
    # como duplicado.
    monkeypatch.setattr(webhook, "_ids_procesados", OrderedDict())

    download = MagicMock(
        return_value=httpx.Response(200, content=audio_bytes, request=httpx.Request("GET", AUDIO_URL))
    )
    monkeypatch.setattr(webhook.httpx, "get", download)
    monkeypatch.setattr(webhook, "WHATSAPP_TOKEN", "test-token")

    # Guardamos qué archivo recibió Whisper (y su contenido) antes de que el
    # webhook lo borre.
    received = {}

    def fake_transcribir(path):
        received["path"] = path
        received["bytes"] = Path(path).read_bytes()
        return "Quiero pedir lo de siempre, pero sin las papas."

    transcribir = MagicMock(side_effect=fake_transcribir)
    monkeypatch.setattr(webhook, "transcribir_audio", transcribir)

    check = MagicMock(return_value={"status": "verified", "student_id": "stu-1"})
    monkeypatch.setattr(webhook, "check_whatsapp_registration", check)

    session = MagicMock()
    session.run_turn.return_value = "¡Listo! Tu pedido sin papas va en camino."
    get_session = MagicMock(return_value=session)
    monkeypatch.setattr(webhook, "get_session", get_session)

    monkeypatch.setattr(webhook, "limit_reached", MagicMock(return_value=False))

    enviar = MagicMock()
    monkeypatch.setattr(webhook, "_enviar_mensaje_whatsapp", enviar)

    return MagicMock(
        audio_bytes=audio_bytes,
        download=download,
        transcribir=transcribir,
        received=received,
        check=check,
        session=session,
        get_session=get_session,
        enviar=enviar,
    )


def test_audio_verificado_se_transcribe_y_responde(client, mocks):
    response = client.post("/", json=_payload())

    assert response.status_code == 200

    # Descarga con el token de Meta.
    mocks.download.assert_called_once()
    args, kwargs = mocks.download.call_args
    assert args[0] == AUDIO_URL
    assert kwargs["headers"] == {"Authorization": "Bearer test-token"}

    # Whisper recibió un .ogg con los bytes descargados, y el temporal se borró.
    assert mocks.received["path"].endswith(".ogg")
    assert mocks.received["bytes"] == mocks.audio_bytes
    assert not os.path.exists(mocks.received["path"])

    # La transcripción llega al orquestador como un mensaje de texto normal.
    mocks.check.assert_called_once_with(WA_ID)
    mocks.session.add_user_message.assert_called_once_with("Quiero pedir lo de siempre, pero sin las papas.")
    mocks.enviar.assert_called_once_with(WA_ID, "¡Listo! Tu pedido sin papas va en camino.")


def test_audio_reenviado_por_meta_se_procesa_una_vez(client, mocks):
    """Meta reenvía el mismo wamid si el 200 tarda: no se debe contestar dos veces."""
    assert client.post("/", json=_payload()).status_code == 200
    assert client.post("/", json=_payload()).status_code == 200

    mocks.download.assert_called_once()
    mocks.transcribir.assert_called_once()
    mocks.session.run_turn.assert_called_once()
    mocks.enviar.assert_called_once()


def test_audios_distintos_se_procesan_los_dos(client, mocks):
    otro = _payload()
    otro["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.OTRO"

    client.post("/", json=_payload())
    client.post("/", json=otro)

    assert mocks.session.run_turn.call_count == 2
    assert mocks.enviar.call_count == 2


def test_estados_de_meta_no_se_procesan(client, mocks):
    """Los avisos sent/delivered/read llegan con field="messages" pero sin "messages"."""
    status_payload = copy.deepcopy(AUDIO_PAYLOAD)
    value = status_payload["entry"][0]["changes"][0]["value"]
    del value["messages"]
    value["statuses"] = [{"id": "wamid.X", "status": "delivered", "recipient_id": WA_ID}]

    response = client.post("/", json=status_payload)

    assert response.status_code == 200
    mocks.download.assert_not_called()
    mocks.enviar.assert_not_called()


def test_sesion_es_por_student_id_no_por_numero(client, mocks):
    mocks.check.return_value = {"status": "verified", "student_id": "stu-1", "full_name": "Diego Duarte"}

    client.post("/", json=_payload())

    mocks.get_session.assert_called_once_with("stu-1", "Diego Duarte")


def test_logs_sin_numero_completo_ni_texto(client, mocks, capsys, monkeypatch):
    monkeypatch.setattr(webhook, "LOG_MESSAGE_CONTENT", False)

    client.post("/", json=_payload())

    logs = capsys.readouterr().out
    assert WA_ID not in logs
    assert "Diego Duarte" not in logs
    assert "sin las papas" not in logs and "va en camino" not in logs
    assert "...8318" in logs


def test_audio_de_usuario_no_registrado_manda_link(client, mocks):
    mocks.check.return_value = {
        "status": "unregistered",
        "message": "Regístrate para poder pedir:",
        "login_url": "https://qorder.app/registro",
    }

    response = client.post("/", json=_payload())

    assert response.status_code == 200
    mocks.transcribir.assert_called_once()
    mocks.session.run_turn.assert_not_called()
    mocks.enviar.assert_called_once_with(
        WA_ID, f"Regístrate para poder pedir:\nhttps://qorder.app/registro?wa={WA_ID}"
    )


def test_audio_sin_url_pide_escribir(client, mocks):
    response = client.post("/", json=_payload({"url": None}))

    assert response.status_code == 200
    mocks.download.assert_not_called()
    mocks.transcribir.assert_not_called()
    mocks.enviar.assert_called_once_with(WA_ID, webhook.AUDIO_ERROR_MESSAGE)


def test_falla_descarga_no_tumba_el_webhook(client, mocks):
    """Si Meta responde 401/404 (token vencido, URL expirada), el webhook
    debe seguir devolviendo 200: si no, Meta reintenta el mismo evento."""
    mocks.download.return_value = httpx.Response(401, request=httpx.Request("GET", AUDIO_URL))

    response = client.post("/", json=_payload())

    assert response.status_code == 200
    mocks.transcribir.assert_not_called()
    mocks.enviar.assert_called_once_with(WA_ID, webhook.AUDIO_ERROR_MESSAGE)


def test_falla_whisper_no_tumba_el_webhook(client, mocks):
    mocks.transcribir.side_effect = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))

    response = client.post("/", json=_payload())

    assert response.status_code == 200
    mocks.session.run_turn.assert_not_called()
    mocks.enviar.assert_called_once_with(WA_ID, webhook.AUDIO_ERROR_MESSAGE)


def test_audio_en_silencio_pide_escribir(client, mocks):
    """Whisper devuelve "" si la nota de voz no tiene habla."""
    mocks.transcribir.side_effect = None
    mocks.transcribir.return_value = ""

    response = client.post("/", json=_payload())

    assert response.status_code == 200
    mocks.session.run_turn.assert_not_called()
    mocks.enviar.assert_called_once_with(WA_ID, webhook.AUDIO_ERROR_MESSAGE)


@pytest.mark.integration
@pytest.mark.skipif(not HAS_REAL_OPENAI_KEY, reason="Falta OPENAI_API_KEY en agent/.env")
@pytest.mark.skipif(not AUDIOS, reason=f"No hay .ogg en {AUDIOS_DIR} (no se suben al repo)")
@pytest.mark.parametrize("audio", AUDIOS, ids=[p.stem for p in AUDIOS])
def test_audio_real_llega_transcrito_al_orquestador(client, mocks, monkeypatch, audio):
    import transcription

    monkeypatch.setattr(webhook, "transcribir_audio", transcription.transcribir_audio)
    mocks.download.return_value = httpx.Response(
        200, content=audio.read_bytes(), request=httpx.Request("GET", AUDIO_URL)
    )

    response = client.post("/", json=_payload())

    assert response.status_code == 200
    mocks.session.add_user_message.assert_called_once()
    texto = mocks.session.add_user_message.call_args.args[0]
    print(f"\n[{audio.name}] -> orquestador recibió: {texto!r}")
    assert texto.strip()
