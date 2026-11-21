"""
Config de pytest para los tests del agente.

Los módulos de agent/ se importan "planos" (from transcription import ...),
igual que cuando se corre uvicorn desde agent/, así que agregamos agent/ al
sys.path.
"""

import os
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
AUDIOS_DIR = Path(__file__).resolve().parent / "audios"

sys.path.insert(0, str(AGENT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(AGENT_DIR / ".env")

# Si no hay key real, guardamos que no la había (para saltar los tests de
# integración) y ponemos una dummy: transcription.py crea el cliente de OpenAI
# al importarse y OpenAI() revienta sin api_key.
HAS_REAL_OPENAI_KEY = bool(os.getenv("OPENAI_API_KEY"))
if not HAS_REAL_OPENAI_KEY:
    os.environ["OPENAI_API_KEY"] = "sk-test-dummy"
