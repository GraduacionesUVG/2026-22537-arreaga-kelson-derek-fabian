# configuration/

Configuración de la aplicación vía `pydantic-settings`. Un único objeto `settings`, importado desde donde se necesite:

```python
from src.configuration import settings

settings.mongo.uri
settings.whatsapp.token
```

## Cómo agregar una variable nueva

1. Agrégala a la clase `Settings` (o a una sub-clase temática como `MongoSettings`, `WhatsAppSettings`, etc.) con su alias de variable de entorno.
2. Documenta el valor por default y agrégala a `.env.example` en la raíz del proyecto.
3. No leas `os.getenv(...)` directamente en otros módulos de `src/` — todo pasa por `settings` para tener una sola fuente de verdad y que `pydantic` valide los tipos.

## Nota sobre `agent/`

El módulo `agent/` (orquestador LLM) maneja sus propias variables de entorno con `load_dotenv()` + `os.getenv()` directamente (`OPENAI_API_KEY`, `ORCHESTRATOR_MODEL`, `TRANSCRIPTION_MODEL`), independiente de este `settings`. Ambos leen el mismo archivo `.env` en la raíz, así que solo hace falta declarar esas variables una vez en `.env.example`.
