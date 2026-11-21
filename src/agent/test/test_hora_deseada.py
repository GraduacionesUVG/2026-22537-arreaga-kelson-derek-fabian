"""
Hora deseada y guardia de crear_pedido del orquestador (agent/orchestator.py).

Sin OpenAI ni backend: las funciones de las tools se reemplazan por falsas, y se llama a
`_execute_tool` igual que lo hace el modelo.

    pytest agent/test/test_hora_deseada.py -v
"""

import json
from datetime import datetime, timedelta

import pytest

import orchestator
from orchestator import GUATEMALA_TZ, ConversationSession, student_asked_for_time
from tools import TOOLS_REGISTRY

RID = "rest-1"
ITEMS = [{"nombre_producto": "Hamburguesa", "cantidad": 1}]


# --- Qué cuenta como "el estudiante pidió una hora" -------------------------------------------


@pytest.mark.parametrize(
    "texto",
    [
        "quiero una hamburguesa doble",
        "Quiero pedir un pastel de chocolate",
        "dame 2 pepsi lata de Golden Harvest",
        "la hamburguesa de Q25 con doble queso",
        "sí, confirmo",
        "lo de siempre por favor",
        "pedido número 5 de la mesa 12",
        "quiero tres hamburguesas y dos pepsis",
        "una de baguette y una de pastel",
        "la de Q15.00 por favor",
        "",
    ],
)
def test_sin_hora(texto):
    assert student_asked_for_time(texto) is False


@pytest.mark.parametrize(
    "texto",
    [
        "quiero una hamburguesa para las 5 pm",
        "a las 5",
        "para la 1:30",
        "lo quiero a las 17:00",
        "para las 12",
        "listo en 20 minutos",
        "en media hora",
        "para el mediodía",
        "a las doce y media",
        "a las 5pm porfa",
        "lo quiero para las 3 p.m.",
        "ahorita mismo",
        "dentro de una hora",
        "PARA LAS 5 PM",
        # horas en letras (audios)
        "a las tres y media",
        "lo quiero tres y media",
        "para las cinco menos cuarto",
        "cuatro en punto",
        "cuarto para las cinco",
        "a las tres de la tarde",
        "ocho de la noche",
        "para las 3 y media",
        "a las 3 y 30",
        "para la una y media",
        "a las quince",
        "A LAS TRES Y MEDIA",
        "para el mediodía",
        "como a las dieciséis",
    ],
)
def test_con_hora(texto):
    assert student_asked_for_time(texto) is True


# --- Fecha actual que recibe el modelo -----------------------------------------------------------


def test_contexto_trae_fecha_y_dia_de_la_semana():
    text = orchestator._now_context()
    now = datetime.now(GUATEMALA_TZ)
    assert f"{now:%Y-%m-%d}" in text
    assert orchestator._WEEKDAYS_ES[now.weekday()] in text
    assert "Guatemala" in text


# --- hora_deseada: solo si la pidió el estudiante, y nunca en el pasado ---------------------------


@pytest.fixture()
def tools(monkeypatch):
    """Tools falsas que registran los parámetros con que fueron llamadas."""
    seen = {"estimar_pedido": [], "crear_pedido": []}

    def estimar(params):
        seen["estimar_pedido"].append(params)
        return {"total_amount": 25.0, "feasible": True, "ready_at": "2026-10-10T20:00:00Z"}

    def crear(params):
        seen["crear_pedido"].append(params)
        return {"id": "order-1"}

    monkeypatch.setitem(TOOLS_REGISTRY["estimar_pedido"], "func", estimar)
    monkeypatch.setitem(TOOLS_REGISTRY["crear_pedido"], "func", crear)
    return seen


def _session(*user_messages: str) -> ConversationSession:
    session = ConversationSession(student_id="20250001")
    for text in user_messages:
        session.add_user_message(text)
    return session


def _args(**extra) -> str:
    return json.dumps({"restaurant_id": RID, "items": ITEMS, **extra})


def _future(hours: float = 2) -> str:
    return (datetime.now(GUATEMALA_TZ) + timedelta(hours=hours)).replace(tzinfo=None).isoformat(timespec="minutes")


def test_hora_que_nadie_pidio_se_descarta(tools):
    session = _session("quiero una hamburguesa")
    result = session._execute_tool("estimar_pedido", _args(hora_deseada=_future()))
    assert "error" not in result
    assert tools["estimar_pedido"][0].hora_deseada is None


def test_hora_pedida_y_futura_se_respeta(tools):
    session = _session("quiero una hamburguesa para las 5 pm")
    hora = _future()
    session._execute_tool("estimar_pedido", _args(hora_deseada=hora))
    assert tools["estimar_pedido"][0].hora_deseada == datetime.fromisoformat(hora)


def test_hora_inventada_en_el_pasado_se_descarta_aunque_la_haya_pedido(tools):
    session = _session("quiero una hamburguesa para las 5 pm")
    session._execute_tool("estimar_pedido", _args(hora_deseada="2023-10-04T17:00:00"))
    assert tools["estimar_pedido"][0].hora_deseada is None


def test_hora_pedida_en_un_mensaje_anterior_sigue_valiendo(tools):
    session = _session("quiero una hamburguesa para las 5 pm", "sí, confirmo")
    hora = _future()
    session._execute_tool("estimar_pedido", _args(hora_deseada=hora))
    assert tools["estimar_pedido"][0].hora_deseada is not None


def test_sin_hora_deseada_no_cambia_nada(tools):
    session = _session("quiero una hamburguesa")
    session._execute_tool("estimar_pedido", _args())
    assert tools["estimar_pedido"][0].hora_deseada is None


def test_descartar_la_hora_no_altera_la_firma_del_pedido(tools):
    """La firma (restaurante + productos) no depende de la hora: estimar con y sin hora es el mismo pedido."""
    session = _session("quiero una hamburguesa")
    session._execute_tool("estimar_pedido", _args(hora_deseada=_future()))
    session.add_user_message("sí, confirmo")
    result = session._execute_tool("crear_pedido", _args(hora_deseada=_future()))
    assert result == {"id": "order-1"}
    assert tools["crear_pedido"][0].hora_deseada is None


# --- Guardia de crear_pedido (human in the loop) -------------------------------------------------


def test_crear_sin_estimar_esta_bloqueado(tools):
    session = _session("quiero una hamburguesa, confirmo")
    result = session._execute_tool("crear_pedido", _args())
    assert "error" in result and tools["crear_pedido"] == []


def test_crear_en_el_mismo_turno_que_se_estima_esta_bloqueado(tools):
    session = _session("quiero una hamburguesa")
    session._execute_tool("estimar_pedido", _args())
    result = session._execute_tool("crear_pedido", _args())
    assert "error" in result and tools["crear_pedido"] == []


def test_crear_en_el_turno_siguiente_funciona(tools):
    session = _session("quiero una hamburguesa")
    session._execute_tool("estimar_pedido", _args())
    session.add_user_message("sí, confirmo")
    assert session._execute_tool("crear_pedido", _args()) == {"id": "order-1"}


def test_crear_otro_producto_distinto_al_estimado_esta_bloqueado(tools):
    session = _session("quiero una hamburguesa")
    session._execute_tool("estimar_pedido", _args())
    session.add_user_message("sí, confirmo")
    otros = json.dumps({"restaurant_id": RID, "items": [{"nombre_producto": "Pepsi", "cantidad": 1}]})
    assert "error" in session._execute_tool("crear_pedido", otros)


def _history_turn(arguments: str, result: dict) -> list[dict]:
    """Un turno guardado en Mongo: mensaje del estudiante, llamada a estimar_pedido y su resultado."""
    return [
        {"role": "user", "content": "quiero una hamburguesa"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "estimar_pedido", "arguments": arguments}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(result)},
        {"role": "assistant", "content": "Total Q25. ¿Confirmas?"},
    ]


def test_sesion_recargada_usa_la_estimacion_del_historial(tools):
    """Reinicio del servicio entre la estimación y la confirmación: _estimates en RAM está vacío."""
    turn = _history_turn(_args(), {"total_amount": 25.0, "feasible": True})
    session = ConversationSession(student_id="20250001", turns=[turn])
    session.add_user_message("sí, confirmo")
    assert session._execute_tool("crear_pedido", _args()) == {"id": "order-1"}


def test_historial_con_estimacion_de_otros_productos_no_habilita(tools):
    otros = json.dumps({"restaurant_id": RID, "items": [{"nombre_producto": "Limonada", "cantidad": 1}]})
    session = ConversationSession(student_id="20250001", turns=[_history_turn(otros, {"total_amount": 8.0})])
    session.add_user_message("sí, confirmo")
    assert "error" in session._execute_tool("crear_pedido", _args())


def test_historial_con_estimacion_fallida_no_habilita(tools):
    turn = _history_turn(_args(), {"error": "'Hamburguesa' no pertenece al menú de hoy"})
    session = ConversationSession(student_id="20250001", turns=[turn])
    session.add_user_message("sí, confirmo")
    assert "error" in session._execute_tool("crear_pedido", _args())


def test_historial_con_otro_restaurante_no_habilita(tools):
    otro = json.dumps({"restaurant_id": "rest-2", "items": ITEMS})
    session = ConversationSession(student_id="20250001", turns=[_history_turn(otro, {"total_amount": 25.0})])
    session.add_user_message("sí, confirmo")
    assert "error" in session._execute_tool("crear_pedido", _args())


# --- Registro de lo que hizo cada tool -----------------------------------------------------------


def test_bloqueo_del_guardia_queda_en_el_log(tools, capsys):
    session = _session("quiero una hamburguesa, confirmo")
    session._execute_tool("crear_pedido", _args())
    out = capsys.readouterr().out
    assert "[tool:crear_pedido] ERROR" in out and "no se puede crear todavía" in out


def test_estimacion_exitosa_deja_feasible_y_hora_en_el_log(tools, capsys):
    session = _session("quiero una hamburguesa")
    session._execute_tool("estimar_pedido", _args(hora_deseada=_future()))
    out = capsys.readouterr().out
    assert "[tool:estimar_pedido] ok" in out and "feasible=True" in out and "no la pidió el estudiante" in out


# --- "tres y media" dicho por la tarde: el modelo suele poner 03:30 ------------------------------


class _FixedNow:
    """Reloj fijo: sábado 2026-10-10, 14:00 en Guatemala."""

    @staticmethod
    def now_gt():
        return datetime(2026, 10, 10, 14, 0, tzinfo=GUATEMALA_TZ)


@pytest.fixture()
def reloj_14h(monkeypatch):
    monkeypatch.setattr(orchestator, "_now_gt", _FixedNow.now_gt)


def test_tres_y_media_a_las_2pm_se_toma_como_la_tarde(tools, reloj_14h):
    session = _session("lo quiero para las tres y media")
    session._execute_tool("estimar_pedido", _args(hora_deseada="2026-10-10T03:30:00"))
    assert tools["estimar_pedido"][0].hora_deseada == datetime(2026, 10, 10, 15, 30)


def test_hora_de_la_manana_ya_pasada_no_se_corre_a_la_tarde(tools, reloj_14h):
    session = _session("lo quiero a las tres y media de la mañana")
    session._execute_tool("estimar_pedido", _args(hora_deseada="2026-10-10T03:30:00"))
    assert tools["estimar_pedido"][0].hora_deseada is None


def test_hora_con_am_explicito_no_se_corre_a_la_tarde(tools, reloj_14h):
    session = _session("para las 3:30 am")
    session._execute_tool("estimar_pedido", _args(hora_deseada="2026-10-10T03:30:00"))
    assert tools["estimar_pedido"][0].hora_deseada is None


def test_hora_pasada_de_la_tarde_se_descarta(tools, reloj_14h):
    """13:30 ya pasó y +12 h cae mañana a la madrugada: no se inventa nada, se ignora."""
    session = _session("lo quiero para las una y media")
    session._execute_tool("estimar_pedido", _args(hora_deseada="2026-10-10T13:30:00"))
    assert tools["estimar_pedido"][0].hora_deseada is None


# --- Cuánto tarda: minutos DESPUÉS del pago ------------------------------------------------------


def test_estimacion_incluye_minutos_despues_del_pago(monkeypatch):
    import tools as tools_module

    respuesta = {"total_amount": 30.0, "order_prep_minutes": 6.0, "queue_backlog_minutes": 3.4, "feasible": True}
    monkeypatch.setattr(tools_module, "backend_request", lambda *a, **k: dict(respuesta))
    params = tools_module.EstimarPedidoParams(restaurant_id=RID, items=ITEMS)
    result = tools_module.estimar_pedido(params)
    assert result["listo_aprox_minutos_despues_del_pago"] == 9  # cola 3.4 + preparación 6
    assert result["total_amount"] == 30.0  # el resto del resultado no cambia


def test_minutos_nunca_menos_de_uno_y_errores_pasan_igual():
    import tools as tools_module

    assert tools_module._con_minutos_despues_del_pago({"order_prep_minutes": 0, "queue_backlog_minutes": 0})[
        "listo_aprox_minutos_despues_del_pago"
    ] == 1
    error = {"error": "no pertenece al menú"}
    assert tools_module._con_minutos_despues_del_pago(error) == error


def test_el_prompt_pide_decir_despues_de_pagar_y_cubre_audios():
    prompt = orchestator.SYSTEM_PROMPT
    assert "listo_aprox_minutos_despues_del_pago" in prompt
    assert "después de que pagues" in prompt
    assert "Audios:" in prompt and "tres y media" in prompt
