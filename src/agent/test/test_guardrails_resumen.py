"""
El resumen del pedido ("Total: Q30.00") no debe confundirse con un producto del menú.

Regresión real: el modelo entendía "una Pepsi botella y un baguette chapin" y estimaba bien, pero el
guardrail rechazaba su respuesta ("Estos productos no aparecen en el menú: Total") y el estudiante
recibía el mensaje de respaldo, como si el agente no hubiera entendido.

    pytest agent/test/test_guardrails_resumen.py -v
"""

import pytest

import guardrails

EVIDENCIA = guardrails.TurnEvidence(
    tool_text="pepsi botella 625ml baguette chapin hamburguesa golden harvest 2026-10-11t21:42:00z",
    prices=frozenset({10.0, 20.0, 25.0, 30.0}),
    minutes=frozenset({15 * 60 + 42}),
    user_text="quiero una pepsi botella y un baguette chapin",
)


def _productos_desconocidos(reply: str) -> list[str]:
    return [v for v in guardrails.check_reply(reply, EVIDENCIA) if "no aparecen en el menú" in v]


@pytest.mark.parametrize(
    "reply",
    [
        "Tu pedido:\n- 1 Pepsi botella 625ml\n- 1 Baguette chapin\nTotal: Q30.00\n¿Confirmas?",
        "Pepsi botella 625ml - Q10\nBaguette chapin - Q20\nTotal - Q30\n¿Lo pido?",
        "1 Pepsi botella 625ml (Q10)\n1 Baguette chapin (Q20)\nTotal a pagar: Q30.00",
        "Subtotal: Q30.00\nTotal: Q30.00",
        "Total del pedido: Q30.00",
    ],
)
def test_total_no_es_un_producto(reply):
    assert _productos_desconocidos(reply) == []


def test_un_producto_inventado_sigue_detectandose():
    reply = "Pepsi botella 625ml - Q10\nPizza suprema - Q45\nTotal: Q55.00"
    violaciones = _productos_desconocidos(reply)
    assert len(violaciones) == 1 and "Pizza suprema" in violaciones[0]
    assert "Total" not in violaciones[0]


def test_producto_que_empieza_con_total_pero_no_existe_se_detecta():
    # "Totalmente vegano" no es una etiqueta de resumen: sigue siendo un producto inventado
    violaciones = _productos_desconocidos("Totalmente vegano - Q40")
    assert len(violaciones) == 1
