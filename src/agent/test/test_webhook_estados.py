"""
Estados de entrega que Meta manda al webhook (sent / delivered / read / failed).

Un "failed" es la única forma de saber POR QUÉ no llegó un mensaje que Meta sí aceptó al enviarlo
(plantilla rechazada al entregar, ventana de 24 h cerrada, límite de marketing, etc.).

    pytest agent/test/test_webhook_estados.py -v
"""

import pytest

import webhook

FALLIDO = {
    "id": "wamid.ABC",
    "status": "failed",
    "timestamp": "1760216061",
    "recipient_id": "50255558318",
    "errors": [
        {
            "code": 131049,
            "title": "This message was not delivered to maintain healthy ecosystem engagement.",
            "message": "Message not delivered",
            "error_data": {"details": "Meta chose not to deliver this marketing template."},
        }
    ],
}


def test_un_envio_fallido_deja_el_motivo_en_el_log(capsys):
    webhook._handle_messages_change({"statuses": [FALLIDO]})
    out = capsys.readouterr().out
    assert "ENTREGA FALLIDA" in out
    assert "131049" in out and "healthy ecosystem engagement" in out
    assert "Meta chose not to deliver this marketing template." in out
    assert "...8318" in out and "50255558318" not in out  # el número va enmascarado


def test_fallido_sin_detalle_usa_el_mensaje_del_error(capsys):
    estado = {**FALLIDO, "errors": [{"code": 131026, "title": "Undeliverable", "message": "Message undeliverable"}]}
    webhook._handle_messages_change({"statuses": [estado]})
    assert "131026" in capsys.readouterr().out


def test_fallido_sin_errores_no_revienta(capsys):
    webhook._handle_messages_change({"statuses": [{"id": "wamid.X", "status": "failed", "recipient_id": "50255558318"}]})
    assert "ENTREGA FALLIDA" in capsys.readouterr().out


@pytest.mark.parametrize("estado", ["sent", "delivered", "read"])
def test_estados_normales_no_hacen_ruido_por_defecto(estado, capsys, monkeypatch):
    monkeypatch.setattr(webhook, "LOG_WA_STATUSES", False)
    webhook._handle_messages_change({"statuses": [{"id": "wamid.X", "status": estado, "recipient_id": "50255558318"}]})
    assert capsys.readouterr().out == ""


def test_con_log_activado_se_imprimen_todos_los_estados(capsys, monkeypatch):
    monkeypatch.setattr(webhook, "LOG_WA_STATUSES", True)
    webhook._handle_messages_change({"statuses": [{"id": "wamid.X", "status": "delivered", "recipient_id": "50255558318"}]})
    out = capsys.readouterr().out
    assert "estado 'delivered'" in out and "...8318" in out


def test_un_cambio_sin_estados_ni_mensajes_no_hace_nada(capsys):
    webhook._handle_messages_change({"metadata": {"phone_number_id": "1"}})
    assert capsys.readouterr().out == ""
