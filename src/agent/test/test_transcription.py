"""
Tests de agent/transcription.py (I-02).

- Unitarios: mockean el cliente de OpenAI, no gastan créditos.
- Integración: mandan los audios reales de agent/test/audios 

Correr desde la raíz del repo:
    pytest agent/test -v -s
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import transcription
from backend_client import BackendError
from conftest import AUDIOS_DIR, HAS_REAL_OPENAI_KEY

AUDIOS = sorted(AUDIOS_DIR.glob("*.ogg"))

RESTAURANTES = [{"name": "Golden Harvest"}, {"name": "Gitane"}]
ITEMS = [
    {"name": "Shuco de salchicha", "available": True},
    {"name": "Papas con queso", "available": True},
    {"name": "Papas con queso", "available": True},
    {"name": "Smoothie de fresa", "available": False},
]


@pytest.fixture(autouse=True)
def vocab_cache_limpia(monkeypatch):
    monkeypatch.setattr(transcription, "_vocab_cache", {"texto": "", "expira": 0.0})


@pytest.fixture
def fake_backend(monkeypatch):
    def responder(method, path, **kwargs):
        return RESTAURANTES if path == "/api/restaurants/" else ITEMS

    backend = MagicMock(side_effect=responder)
    monkeypatch.setattr(transcription, "backend_request", backend)
    return backend


@pytest.fixture
def fake_client(monkeypatch, fake_backend):
    client = MagicMock()
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="Quiero un café")
    monkeypatch.setattr(transcription, "client", client)
    return client


@pytest.fixture
def audio_path(tmp_path):
    """Los unitarios no necesitan un audio real (Whisper está mockeado)."""
    path = tmp_path / "nota_de_voz.ogg"
    path.write_bytes(b"OggS fake audio")
    return str(path)


def test_transcribir_audio_devuelve_texto(fake_client, audio_path):
    texto = transcription.transcribir_audio(audio_path)

    assert texto == "Quiero un café"
    fake_client.audio.transcriptions.create.assert_called_once()


def test_transcribir_audio_manda_modelo_y_archivo(fake_client, audio_path):
    transcription.transcribir_audio(audio_path)

    kwargs = fake_client.audio.transcriptions.create.call_args.kwargs
    assert kwargs["model"] == transcription.TRANSCRIPTION_MODEL
    # Whisper usa la extensión del nombre para detectar el formato.
    assert kwargs["file"].name.endswith(".ogg")
    assert kwargs["language"] == "es"
    assert kwargs["prompt"].startswith(transcription.BASE_PROMPT)


def test_sin_idioma_whisper_autodetecta(fake_client, audio_path, monkeypatch):
    monkeypatch.setattr(transcription, "TRANSCRIPTION_LANGUAGE", "")

    transcription.transcribir_audio(audio_path)

    assert "language" not in fake_client.audio.transcriptions.create.call_args.kwargs


def test_prompt_base_tiene_ejemplos_de_spanglish():
    for palabra in ("smoothies", "combo", "cheese", "please", "pickear"):
        assert palabra in transcription.BASE_PROMPT


def test_prompt_incluye_restaurantes_y_platillos_disponibles(fake_backend):
    prompt = transcription.construir_prompt()

    assert "Golden Harvest" in prompt
    assert "Shuco de salchicha" in prompt
    assert prompt.count("Papas con queso") == 1
    assert "Smoothie de fresa" not in prompt


def test_prompt_no_pasa_del_limite(fake_backend):
    fake_backend.side_effect = lambda method, path, **kw: (
        [] if path == "/api/restaurants/" else [{"name": f"Platillo {n}"} for n in range(500)]
    )

    prompt = transcription.construir_prompt()

    assert len(prompt) <= transcription.MAX_PROMPT_CHARS
    assert prompt.startswith(transcription.BASE_PROMPT)


def test_menu_se_cachea(fake_backend):
    transcription.construir_prompt()
    transcription.construir_prompt()

    # 2 requests (restaurantes + menús) solo la primera vez.
    assert fake_backend.call_count == 2


@pytest.mark.parametrize(
    "error",
    [BackendError("Error interno", 500), httpx.ConnectError("backend dormido")],
    ids=["backend_500", "backend_caido"],
)
def test_backend_falla_usa_prompt_base(fake_backend, error):
    fake_backend.side_effect = error

    assert transcription.construir_prompt() == transcription.BASE_PROMPT


def test_backend_falla_conserva_vocabulario_anterior(fake_backend, monkeypatch):
    transcription.construir_prompt()
    # Venció la caché y ahora el backend no responde.
    transcription._vocab_cache["expira"] = 0.0
    fake_backend.side_effect = httpx.ConnectError("backend dormido")

    assert "Golden Harvest" in transcription.construir_prompt()


def test_transcribir_audio_archivo_inexistente(fake_client):
    with pytest.raises(FileNotFoundError):
        transcription.transcribir_audio("no_existe.ogg")
    fake_client.audio.transcriptions.create.assert_not_called()


@pytest.mark.integration
@pytest.mark.skipif(not HAS_REAL_OPENAI_KEY, reason="Falta OPENAI_API_KEY en agent/.env")
@pytest.mark.skipif(not AUDIOS, reason=f"No hay .ogg en {AUDIOS_DIR} (no se suben al repo)")
@pytest.mark.parametrize("audio", AUDIOS, ids=[p.stem for p in AUDIOS])
def test_transcribir_audio_real(audio):
    texto = transcription.transcribir_audio(str(audio))

    print(f"\n[{audio.name}] -> {texto!r}")
    assert isinstance(texto, str)
    assert texto.strip()
