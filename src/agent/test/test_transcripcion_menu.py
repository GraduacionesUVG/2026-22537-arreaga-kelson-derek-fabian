"""
Corrección fonética de las transcripciones contra el menú (agent/transcription.py).

Whisper está mockeado: no gasta créditos ni necesita audios.

    pytest agent/test/test_transcripcion_menu.py -v
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import transcription

MENU = [
    "Golden Harvest",
    "Baguette chapin",
    "Pepsi lata 355ml",
    "Pepsi botella 625ml",
    "Pastel de Chocolate",
    "Hamburguesa",
    "Jugo Petit Manzana 330ml",
    "Papas con queso",
    "Shuco de salchicha",
    "Limonada",
]


@pytest.mark.parametrize(
    ("dicho", "esperado"),
    [
        # el caso reportado: "baguette" mal transcrito
        ("quiero un baget chapin y una pepsi en botella", "quiero un Baguette chapin y una pepsi en botella"),
        ("dame un bagüet chapin", "dame un Baguette chapin"),
        ("un vaguete chapin por favor", "un Baguette chapin por favor"),
        ("quiero un bagette chapin", "quiero un Baguette chapin"),
        ("quiero una hamburgesa", "quiero una Hamburguesa"),
        ("un suco de salchicha", "un Shuco de salchicha"),
        ("una limonda", "una Limonada"),
    ],
)
def test_corrige_nombres_mal_transcritos(dicho, esperado):
    assert transcription.corregir_con_menu(dicho, MENU) == esperado


@pytest.mark.parametrize(
    "texto",
    [
        "hola buenas tardes",
        "quiero un baguette chapin",  # ya bien escrito
        "dos pasteles de chocolate",  # plural: no es un error
        "dos limonadas y una pepsi",
        "quiso una pepsi",  # no convertir en "queso"
        "me das un jugo de manzana",
        "lo quiero para las tres y media",
        "",
    ],
)
def test_no_toca_lo_que_esta_bien(texto):
    assert transcription.corregir_con_menu(texto, MENU) == texto


def test_sin_menu_devuelve_el_texto_igual():
    assert transcription.corregir_con_menu("un baget chapin", []) == "un baget chapin"


def test_no_inventa_a_partir_de_palabras_cortas():
    # "pan" / "papa" son más cortas que el mínimo: nunca se tocan
    assert transcription.corregir_con_menu("un pan con papa", ["Baguette chapin", "Papas con queso"]) == "un pan con papa"


# --- Integración con transcribir_audio ---------------------------------------------------------


@pytest.fixture()
def whisper(monkeypatch, tmp_path):
    cache = {"texto": ", ".join(MENU), "nombres": list(MENU), "expira": time.monotonic() + 1000}
    monkeypatch.setattr(transcription, "_vocab_cache", cache)
    client = MagicMock()
    monkeypatch.setattr(transcription, "client", client)
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(b"OggS fake")
    return client, str(audio)


def test_transcribir_audio_devuelve_el_texto_corregido(whisper):
    client, audio = whisper
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="Quiero un baget chapin")
    assert transcription.transcribir_audio(audio) == "Quiero un Baguette chapin"


def test_transcribir_audio_manda_los_nombres_del_menu_a_whisper(whisper):
    client, audio = whisper
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="hola")
    transcription.transcribir_audio(audio)
    prompt = client.audio.transcriptions.create.call_args.kwargs["prompt"]
    assert "Baguette chapin" in prompt


def test_el_texto_completo_solo_se_imprime_si_se_pide(whisper, monkeypatch, capsys):
    client, audio = whisper
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="Quiero un baguette chapin")

    monkeypatch.delenv("LOG_TRANSCRIPTS", raising=False)
    transcription.transcribir_audio(audio)
    assert "texto:" not in capsys.readouterr().out

    monkeypatch.setenv("LOG_TRANSCRIPTS", "1")
    transcription.transcribir_audio(audio)
    assert "texto: 'Quiero un baguette chapin'" in capsys.readouterr().out
