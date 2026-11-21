"""
Procesamiento de Voz (STT) - I-02

Wrapper simple sobre Whisper (OpenAI) para transcribir los audios de WhatsApp
antes de pasarlos al Orquestador LLM. webhook.py descarga la nota de voz a un
archivo temporal y llama a transcribir_audio con la ruta.

Para que Whisper escriba bien los nombres locales ("shuco", no "shuko") se le
pasa un `prompt` con vocabulario: una base fija + los nombres reales de
restaurantes y platillos del backend (cacheados, ver _vocabulario_menu).
"""

import os
import re
import time
import unicodedata
from difflib import SequenceMatcher

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from backend_client import BackendError, backend_request

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "whisper-1")
# Fijar el idioma
TRANSCRIPTION_LANGUAGE = os.getenv("TRANSCRIPTION_LANGUAGE", "es")

BASE_PROMPT = (
    "Nota de voz de un estudiante en Guatemala pidiendo comida por WhatsApp, "
    "a veces en spanglish: quiero un shuco y unos chuchitos; ¿hay smoothies? "
    "Dame el combo con extra cheese, please. Lo paso a pickear a las 2 de la "
    "tarde, son Q25, ok?"
)

MAX_PROMPT_CHARS = 700

VOCAB_TTL_SECONDS = 600
VOCAB_RETRY_SECONDS = 60
_vocab_cache = {"texto": "", "nombres": [], "expira": 0.0}


def _vocabulario_menu() -> str:
    if time.monotonic() < _vocab_cache["expira"]:
        return _vocab_cache["texto"]

    try:
        restaurantes = backend_request("GET", "/api/restaurants/", params={"active_only": True})
        items = backend_request("GET", "/api/menus/")
    except (BackendError, httpx.HTTPError) as exc:
        print(f"[transcription] No se pudo cargar el menú para el prompt de Whisper: {exc}")
        _vocab_cache["expira"] = time.monotonic() + VOCAB_RETRY_SECONDS
        return _vocab_cache["texto"]

    nombres = [r["name"] for r in restaurantes]
    nombres += [i["name"] for i in items if i.get("available", True)]

    unicos = list(dict.fromkeys(nombres))
    _vocab_cache["texto"] = ", ".join(unicos)
    _vocab_cache["nombres"] = unicos
    _vocab_cache["expira"] = time.monotonic() + VOCAB_TTL_SECONDS
    return _vocab_cache["texto"]


def _nombres_menu() -> list[str]:
    """Nombres reales de restaurantes y platillos (misma caché que el prompt de Whisper)."""
    _vocabulario_menu()
    return list(_vocab_cache.get("nombres", []))


# --- Corrección fonética contra el menú ---------------------------------------------------------
# Whisper con `prompt` ayuda, pero el prompt solo "ve" ~700 caracteres: en un menú grande los
# platillos del final no caben ("baguette chapin" sale como "baget chapin"). Esta pasada compara
# cada palabra del audio con las palabras distintivas del menú y corrige las que suenan casi igual.

_PALABRAS_COMUNES = {"para", "con", "sin", "una", "unos", "unas", "los", "las", "del", "por", "mas", "extra"}
_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _fonetica(palabra: str) -> str:
    """Forma comparable: minúsculas, sin acentos y con las confusiones típicas del español (b/v, z/s, h)."""
    texto = unicodedata.normalize("NFD", palabra.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.replace("v", "b").replace("z", "s").replace("h", "")


def _sin_acentos(palabra: str) -> str:
    texto = unicodedata.normalize("NFD", palabra.lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _umbral(palabra: str) -> float:
    # Palabras cortas: más estricto, para no convertir "quiso" (0.80) en "queso".
    return 0.81 if len(palabra) < 7 else 0.75


def _es_plural_de(a: str, b: str) -> bool:
    """¿Una es la otra más "s"/"es"? ("pasteles" / "pastel"): variante, no error de transcripción."""
    return a in (b + "s", b + "es") or b in (a + "s", a + "es")


def corregir_con_menu(texto: str, nombres: list[str] | None = None) -> str:
    """Reemplaza palabras mal transcritas por la palabra del menú que suena casi igual."""
    nombres = _nombres_menu() if nombres is None else nombres
    if not texto or not nombres:
        return texto

    # fonética -> palabra tal como está en el menú
    vocabulario: dict[str, str] = {}
    for nombre in nombres:
        for palabra in _TOKEN_RE.findall(nombre):
            if len(palabra) >= 5 and palabra.lower() not in _PALABRAS_COMUNES:
                vocabulario.setdefault(_fonetica(palabra), palabra)
    if not vocabulario:
        return texto

    def corregir(match: re.Match) -> str:
        original = match.group(0)
        forma = _fonetica(original)
        if forma in vocabulario:
            # Suena igual que una palabra del menú: se deja con la ortografía del menú ("suco" -> "shuco").
            canonica = vocabulario[forma]
            if _sin_acentos(original) != _sin_acentos(canonica):
                print(f"[transcription] corregido por menú: '{original}' -> '{canonica}'")
                return canonica
            return original
        if len(original) < 5:
            return original
        mejor, mejor_ratio = None, 0.0
        for candidata_forma, candidata in vocabulario.items():
            if forma[:1] != candidata_forma[:1] or abs(len(forma) - len(candidata_forma)) > 3:
                continue
            if _es_plural_de(forma, candidata_forma):
                continue
            ratio = SequenceMatcher(None, forma, candidata_forma).ratio()
            if ratio > mejor_ratio:
                mejor, mejor_ratio = candidata, ratio
        if mejor is not None and mejor_ratio >= _umbral(mejor):
            print(f"[transcription] corregido por menú: '{original}' -> '{mejor}'")
            return mejor
        return original

    return _TOKEN_RE.sub(corregir, texto)


def construir_prompt() -> str:
    vocabulario = _vocabulario_menu()
    prompt = f"{BASE_PROMPT} Restaurantes y platillos: {vocabulario}." if vocabulario else BASE_PROMPT
    return prompt[:MAX_PROMPT_CHARS]


def transcribir_audio(ruta_archivo: str) -> str:
    extra = {"language": TRANSCRIPTION_LANGUAGE} if TRANSCRIPTION_LANGUAGE else {}
    with open(ruta_archivo, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model=TRANSCRIPTION_MODEL,
            file=audio_file,
            prompt=construir_prompt(),
            **extra,
        )
    texto = corregir_con_menu(transcription.text)
    if os.getenv("LOG_TRANSCRIPTS", "").lower() in ("1", "true"):
        # Apagado por defecto: es el contenido del mensaje del estudiante.
        print(f"[transcription] texto: {texto!r}")
    return texto
