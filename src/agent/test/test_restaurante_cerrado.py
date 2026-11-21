"""
Restaurante cerrado y pedido no viable en el orquestador (agent/orchestator.py).

Sin OpenAI ni backend: las tools se reemplazan por falsas y el estado del restaurante llega como
si lo hubieran devuelto listar_restaurantes / ver_menu (mensajes de tool en el historial).

    pytest agent/test/test_restaurante_cerrado.py -v
"""

import json
from datetime import datetime, timedelta

import pytest

from orchestator import GUATEMALA_TZ, ConversationSession
from tools import TOOLS_REGISTRY

RID = "6ab9be9dc89b7fec1c409fed"
ITEMS = [{"nombre_producto": "Hamburguesa", "cantidad": 1}]
CLOSED_MSG = "Golden Harvest está cerrado en este momento. Abre mañana a las 9:00 am."


@pytest.fixture()
def tools(monkeypatch):
    seen = {"estimar_pedido": [], "crear_pedido": [], "ver_menu": []}
    state = {"feasible": True}

    def estimar(params):
        seen["estimar_pedido"].append(params)
        return {"total_amount": 25.0, "feasible": state["feasible"], "ready_at": "2026-10-10T20:00:00Z"}

    def crear(params):
        seen["crear_pedido"].append(params)
        return {"id": "order-1"}

    def menu(params):
        seen["ver_menu"].append(params)
        return {"items": []}

    monkeypatch.setitem(TOOLS_REGISTRY["estimar_pedido"], "func", estimar)
    monkeypatch.setitem(TOOLS_REGISTRY["crear_pedido"], "func", crear)
    monkeypatch.setitem(TOOLS_REGISTRY["ver_menu"], "func", menu)
    # Sin datos frescos el filtro consulta listar_restaurantes en vivo: aquí devuelve "sin datos".
    monkeypatch.setitem(TOOLS_REGISTRY["listar_restaurantes"], "func", lambda params: {"restaurantes": []})
    seen["state"] = state
    return seen


def _args(**extra) -> str:
    return json.dumps({"restaurant_id": RID, "items": ITEMS, **extra})


def _session(*user_messages: str) -> ConversationSession:
    session = ConversationSession(student_id="20250001")
    for text in user_messages:
        session.add_user_message(text)
    return session


def _tool_result(session: ConversationSession, call_id: str, name: str, result: dict) -> None:
    """Agrega al historial una llamada a tool con su resultado, como la dejaría el modelo."""
    session.messages.append(
        {
            "role": "assistant",
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}],
        }
    )
    session.messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})


def _listado(status: str = "closed") -> dict:
    abierto = status == "open"
    return {
        "restaurantes": [
            {
                "id": RID,
                "name": "Golden Harvest",
                "status": status,
                "is_open_now": abierto,
                "status_message": None if abierto else CLOSED_MSG,
            }
        ]
    }


def _menu(status: str = "closed") -> dict:
    return {
        "items": [
            {
                "restaurant_id": RID,
                "name": "Hamburguesa",
                "price": 25,
                "restaurant_status": status,
                "restaurant_status_message": None if status == "open" else CLOSED_MSG,
            }
        ]
    }


def _future(hours: float = 2) -> str:
    return (datetime.now(GUATEMALA_TZ) + timedelta(hours=hours)).replace(tzinfo=None).isoformat(timespec="minutes")


# --- Cerrado según listar_restaurantes ------------------------------------------------------------


def test_cerrado_en_listado_bloquea_estimar(tools):
    session = _session("quiero una hamburguesa")
    _tool_result(session, "c1", "listar_restaurantes", _listado("closed"))
    result = session._execute_tool("estimar_pedido", _args())
    assert "cerrado" in result["error"] and CLOSED_MSG in result["error"]
    assert tools["estimar_pedido"] == []


def test_cerrado_en_listado_bloquea_ver_menu(tools):
    session = _session("qué tienen")
    _tool_result(session, "c1", "listar_restaurantes", _listado("closed"))
    result = session._execute_tool("ver_menu", json.dumps({"restaurant_id": RID}))
    assert "error" in result and tools["ver_menu"] == []


def test_cerrado_bloquea_crear_aunque_ya_se_haya_estimado(tools):
    """Se estimó cuando estaba abierto; al confirmar ya cerró."""
    session = _session("quiero una hamburguesa")
    _tool_result(session, "c1", "listar_restaurantes", _listado("open"))
    session._execute_tool("estimar_pedido", _args())
    session.add_user_message("sí, confirmo")
    _tool_result(session, "c2", "listar_restaurantes", _listado("closed"))
    result = session._execute_tool("crear_pedido", _args())
    assert "error" in result and tools["crear_pedido"] == []


def test_abierto_en_listado_deja_pasar(tools):
    session = _session("quiero una hamburguesa")
    _tool_result(session, "c1", "listar_restaurantes", _listado("open"))
    result = session._execute_tool("estimar_pedido", _args())
    assert "error" not in result and len(tools["estimar_pedido"]) == 1


# --- Cerrado según ver_menu (restaurant_status en cada item) --------------------------------------


def test_cerrado_en_menu_bloquea_estimar(tools):
    session = _session("quiero una hamburguesa")
    _tool_result(session, "c1", "ver_menu", _menu("closed"))
    result = session._execute_tool("estimar_pedido", _args())
    assert CLOSED_MSG in result["error"] and tools["estimar_pedido"] == []


def test_cerrado_en_menu_bloquea_crear(tools):
    session = _session("quiero una hamburguesa")
    _tool_result(session, "c1", "ver_menu", _menu("open"))
    session._execute_tool("estimar_pedido", _args())
    session.add_user_message("sí, confirmo")
    _tool_result(session, "c2", "ver_menu", _menu("closed"))
    result = session._execute_tool("crear_pedido", _args())
    assert "error" in result and tools["crear_pedido"] == []


def test_abre_despues_ya_no_bloquea(tools):
    """Manda el dato más reciente: estaba cerrado, luego abrió."""
    session = _session("quiero una hamburguesa")
    _tool_result(session, "c1", "listar_restaurantes", _listado("closed"))
    _tool_result(session, "c2", "listar_restaurantes", _listado("open"))
    result = session._execute_tool("estimar_pedido", _args())
    assert "error" not in result


def test_otro_restaurante_cerrado_no_afecta(tools):
    session = _session("quiero una hamburguesa")
    otro = _listado("closed")
    otro["restaurantes"][0]["id"] = "otro-restaurante"
    _tool_result(session, "c1", "listar_restaurantes", otro)
    result = session._execute_tool("estimar_pedido", _args())
    assert "error" not in result


def test_sin_datos_de_estado_no_bloquea(tools):
    """Sin listado ni menú el agente no inventa un cierre: lo decide el backend (409)."""
    session = _session("quiero una hamburguesa")
    result = session._execute_tool("estimar_pedido", _args())
    assert "error" not in result


# --- Pedido no viable (feasible=false) ------------------------------------------------------------


def test_no_viable_bloquea_crear(tools):
    tools["state"]["feasible"] = False
    session = _session("quiero una hamburguesa para las 5 pm")
    hora = _future()
    estimado = session._execute_tool("estimar_pedido", _args(hora_deseada=hora))
    assert estimado["feasible"] is False
    session.add_user_message("sí, confirmo")
    result = session._execute_tool("crear_pedido", _args(hora_deseada=hora))
    assert "NO viable" in result["error"] and tools["crear_pedido"] == []


def test_otra_hora_viable_permite_crear(tools):
    tools["state"]["feasible"] = False
    session = _session("quiero una hamburguesa para las 5 pm")
    session._execute_tool("estimar_pedido", _args(hora_deseada=_future(2)))
    tools["state"]["feasible"] = True
    session.add_user_message("mejor para las 6 pm")
    hora2 = _future(3)
    session._execute_tool("estimar_pedido", _args(hora_deseada=hora2))
    session.add_user_message("sí, confirmo")
    assert session._execute_tool("crear_pedido", _args(hora_deseada=hora2)) == {"id": "order-1"}
