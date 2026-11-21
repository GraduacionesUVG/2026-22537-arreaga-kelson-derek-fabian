"""
Memoria del agente de WhatsApp por estudiante (`user_memory`).

Un documento por estudiante, llave `student_id` (la misma de `users` y
`orders`). Lo lee y escribe solo `agent/` vía /api/users/{student_id}/memory
(protegido con INTERNAL_API_KEY). Ejemplo tal como queda en Mongo:

    {
        "_id": ObjectId("..."),
        "student_id": "5024-carne-demo",
        "schema_version": 1,
        "preferences": {
            "likes": ["Hamburguesa"],
            "dislikes": ["papas"],
            "restrictions": ["vegetariano"],
            "usual_budget": 20.0
        },
        "summaries": [
            {"date": ISODate("2026-09-27T21:20:00Z"), "text": "Preguntó por smoothies, no pidió."}
        ],
        "session": {
            "started_at": ISODate("2026-09-28T18:00:00Z"),
            "last_message_at": ISODate("2026-09-28T18:05:00Z"),
            "turns": [[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]]
        },
        "created_at": ISODate("2026-09-27T21:19:00Z"),
        "updated_at": ISODate("2026-09-28T18:05:00Z")
    }

Pensado para crecer:
- Cada sección es un subdocumento: una sección nueva se agrega al schema
  con default vacío y los documentos viejos se leen igual (normalize_user_memory).
- Todos los arrays tienen tope (ver límites abajo). Lo que crezca sin tope
  (ej. archivar todas las conversaciones) va en su propia colección, no aquí.
- `schema_version`: si el formato cambia, se agrega una migración en
  `_MIGRATIONS` y se aplica al leer (migración perezosa, sin scripts masivos).
- `updated_at` lleva un índice TTL (INACTIVE_USER_TTL_DAYS, igual que `users`).
"""

COLLECTION_NAME = "user_memory"
CURRENT_SCHEMA_VERSION = 1

# Topes -- mantienen el documento chico (se carga en cada mensaje) y acotan lo
# que se le manda a OpenAI.
MAX_LIST_ITEMS = 10
MAX_ITEM_CHARS = 60
MAX_SUMMARIES = 3
MAX_SUMMARY_CHARS = 300
MAX_TURNS = 8

# version_origen -> función que lleva el documento a version_origen + 1.
# Ej. cuando exista la v2: _MIGRATIONS = {1: _v1_to_v2}
_MIGRATIONS: dict = {}


def empty_user_memory(student_id: str) -> dict:
    return {
        "student_id": student_id,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "preferences": {"likes": [], "dislikes": [], "restrictions": [], "usual_budget": None},
        "summaries": [],
        "session": {"started_at": None, "last_message_at": None, "turns": []},
        "created_at": None,
        "updated_at": None,
    }


def normalize_user_memory(doc: dict | None, student_id: str) -> dict:
    """Lleva cualquier documento (o ninguno) a la forma actual: aplica
    migraciones pendientes y rellena con defaults las secciones/campos que
    falten, sin tocar lo que ya tenga."""
    base = empty_user_memory(student_id)
    if not doc:
        return base

    doc = dict(doc)
    version = doc.get("schema_version") or 1
    while version < CURRENT_SCHEMA_VERSION:
        doc = _MIGRATIONS[version](doc)
        version += 1

    for key, default in base.items():
        if isinstance(default, dict):
            doc[key] = {**default, **(doc.get(key) or {})}
        elif doc.get(key) is None:
            doc[key] = default
    doc["schema_version"] = CURRENT_SCHEMA_VERSION
    return doc
