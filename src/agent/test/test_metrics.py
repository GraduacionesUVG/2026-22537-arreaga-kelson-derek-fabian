"""
Tiempos de conversación de un pedido (agent/metrics.py) y su cableado en orchestator.py.

    pytest agent/test/test_metrics.py -v
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import orchestator
from metrics import OrderMetrics, meta_timestamp


def test_meta_timestamp():
    assert meta_timestamp("1760972400") == datetime(2025, 10, 20, 15, 0, tzinfo=timezone.utc)
    assert meta_timestamp(None) is None
    assert meta_timestamp("no-es-numero") is None


def test_sin_pedido_no_hay_payload():
    m = OrderMetrics()
    m.begin_turn(None, "text")
    m.end_turn()
    assert m.payload() is None


def test_payload_acumula_turnos_y_reset_reinicia():
    m = OrderMetrics()
    sent = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    m.begin_turn(sent, "audio", audio_ms=1800)
    m.add_llm(900)
    m.add_tool("estimar_pedido", 250)
    m.end_turn()
    m.begin_turn(datetime(2026, 10, 20, 15, 0, 20, tzinfo=timezone.utc), "text")
    m.add_llm(700)
    m.add_tool("crear_pedido", 400)
    m.mark_order_created("o-1")
    m.end_turn()

    p = m.payload()
    assert p["order_id"] == "o-1"
    assert p["first_message_at"] == sent.isoformat()  # el del PRIMER mensaje, no el último
    assert [t["input_type"] for t in p["turns"]] == ["audio", "text"]
    assert p["turns"][0]["audio_ms"] == 1800 and p["turns"][0]["tools"] == ["estimar_pedido"]
    assert p["turns"][1]["llm_ms"] == 700

    m.reset()
    assert m.payload() is None and m.first_message_at is None


def test_orquestador_mide_tools_y_detecta_pedido(monkeypatch):
    """crear_pedido OK -> mark_order_created con el id que devolvió el backend; con error, no."""
    results = iter([{"error": "sin stock"}, {"id": "ord-9"}])
    monkeypatch.setattr(orchestator.ConversationSession, "_run_tool", lambda self, n, a: next(results))

    s = orchestator.ConversationSession(student_id="x")
    s.metrics.begin_turn(None, "text")
    s._execute_tool("crear_pedido", "{}")
    assert s.metrics.order_id is None  # el primer intento falló
    s._execute_tool("crear_pedido", "{}")
    s.metrics.end_turn()

    p = s.metrics.payload()
    assert p["order_id"] == "ord-9"
    assert p["turns"][0]["tools"] == ["crear_pedido", "crear_pedido"]
    assert p["turns"][0]["tools_ms"] >= 0


def test_orquestador_mide_llm(monkeypatch):
    fake = MagicMock()
    fake.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None, content="Hola"))]
    )
    monkeypatch.setattr(orchestator, "client", fake)

    s = orchestator.ConversationSession(student_id="x")
    s.metrics.begin_turn(None, "text")
    assert s._run_turn() == "Hola"
    assert s.metrics._turn["llm_ms"] >= 0 and fake.chat.completions.create.call_count == 1
