"""Repositorio de estudiantes (`users`, ver src/models/user.py).

El número de WhatsApp se guarda cifrado (ver src/core/pii.py):

- Al escribir, `whatsapp_number` se convierte en `whatsapp_number_enc`
  (cifrado) + `whatsapp_number_hash` (para buscar) y se quita el campo en
  claro.
- Al leer, se descifra y se devuelve otra vez como `whatsapp_number`, así
  que los servicios (registro, OTP, avisos de pedidos) no cambian. Los
  campos `*_enc` / `*_hash` nunca salen de este repositorio.

Compatibilidad con documentos viejos (número en claro): se siguen
encontrando y leyendo, y se cifran en cuanto se leen. Para cifrarlos todos de
una vez: `uv run python -m scripts.encrypt_user_phone_numbers --apply`.
"""

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING

from src.core.pii import (
    PiiCryptoError,
    decrypt_value,
    encrypt_value,
    whatsapp_number_hash,
)
from src.models.user import COLLECTION_NAME
from src.repositories.base_repo import BaseRepository, serialize_document, to_object_id

logger = logging.getLogger(__name__)

PLAIN_FIELD = "whatsapp_number"
ENC_FIELD = "whatsapp_number_enc"
HASH_FIELD = "whatsapp_number_hash"
LEGACY_INDEX = "uniq_whatsapp_number"
HASH_INDEX = "uniq_whatsapp_number_hash"


def protect_user_fields(data: dict) -> dict:
    """Copia de `data` lista para Mongo: reemplaza `whatsapp_number` por su versión cifrada."""
    data = dict(data)
    if PLAIN_FIELD in data:
        number = data.pop(PLAIN_FIELD)
        # Sin número no se escribe nada: el índice es `sparse` y un `null` sí contaría como valor.
        if number:
            data[ENC_FIELD] = encrypt_value(number)
            data[HASH_FIELD] = whatsapp_number_hash(number)
    return data


def reveal_user_fields(doc: dict | None) -> dict | None:
    """Documento ya serializado -> mismo documento con `whatsapp_number` en claro."""
    if doc is None:
        return None
    doc = dict(doc)
    token = doc.pop(ENC_FIELD, None)
    doc.pop(HASH_FIELD, None)
    if token:
        try:
            doc[PLAIN_FIELD] = decrypt_value(token)
        except PiiCryptoError:
            logger.error("Usuario %s: no se pudo descifrar el número de WhatsApp.", doc.get("id"))
            doc[PLAIN_FIELD] = None
    else:
        doc.setdefault(PLAIN_FIELD, None)
    return doc


def encryption_update(number: str) -> dict:
    """Update de Mongo que cifra el número de un documento viejo y borra el campo en claro."""
    return {
        "$set": {ENC_FIELD: encrypt_value(number), HASH_FIELD: whatsapp_number_hash(number)},
        "$unset": {PLAIN_FIELD: ""},
    }


async def ensure_user_phone_indexes(db) -> None:
    """Idempotente. Quita el índice único sobre el número en claro (si se queda, el
    segundo usuario sin ese campo choca como `null` duplicado) y crea el del hash."""
    collection = db[COLLECTION_NAME]
    existing = await collection.index_information()
    if LEGACY_INDEX in existing:
        await collection.drop_index(LEGACY_INDEX)
        logger.info("users: se eliminó el índice %s (número en claro).", LEGACY_INDEX)
    if HASH_INDEX not in existing:
        await collection.create_index(
            [(HASH_FIELD, ASCENDING)],
            unique=True,
            name=HASH_INDEX,
            sparse=True,  # los documentos viejos aún sin cifrar no tienen el campo
        )


class UserRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def _upgrade_legacy(self, raw: dict | None) -> None:
        """Si el documento todavía tiene el número en claro, lo cifra en Mongo."""
        if raw and raw.get(PLAIN_FIELD) and not raw.get(ENC_FIELD):
            try:
                await self.collection.update_one({"_id": raw["_id"]}, encryption_update(raw[PLAIN_FIELD]))
            except Exception:  # no debe frenar la lectura
                logger.exception("Usuario %s: no se pudo cifrar el número guardado en claro.", raw["_id"])

    async def _find_raw(self, filter_: dict) -> dict | None:
        raw = await self.collection.find_one(filter_)
        await self._upgrade_legacy(raw)
        return reveal_user_fields(serialize_document(raw))

    # --- CRUD genérico con cifrado -------------------------------------------

    async def find_by_id(self, id_str: str) -> dict | None:
        return reveal_user_fields(await super().find_by_id(id_str))

    async def find_one(self, filter_: dict) -> dict | None:
        return reveal_user_fields(await super().find_one(filter_))

    async def find_many(self, filter_: dict | None = None, limit: int = 100) -> list[dict]:
        return [reveal_user_fields(d) for d in await super().find_many(filter_, limit)]

    async def find_all(self, filter_: dict | None = None, sort: list | None = None) -> list[dict]:
        return [reveal_user_fields(d) for d in await super().find_all(filter_, sort)]

    async def insert_one(self, data: dict) -> dict:
        return reveal_user_fields(await super().insert_one(protect_user_fields(data)))

    async def update_one(self, id_str: str, data: dict) -> dict | None:
        update: dict = {"$set": protect_user_fields(data)}
        if PLAIN_FIELD in data:
            update["$unset"] = {PLAIN_FIELD: ""}
        await self.collection.update_one({"_id": to_object_id(id_str)}, update)
        return await self.find_by_id(id_str)

    async def update_where(self, id_str: str, conditions: dict, data: dict) -> dict | None:
        return reveal_user_fields(await super().update_where(id_str, conditions, protect_user_fields(data)))

    # --- Búsquedas ---------------------------------------------------------------

    async def find_by_whatsapp_number(self, whatsapp_number: str) -> dict | None:
        # El segundo criterio solo encuentra documentos viejos aún sin cifrar.
        return await self._find_raw(
            {"$or": [{HASH_FIELD: whatsapp_number_hash(whatsapp_number)}, {PLAIN_FIELD: whatsapp_number}]}
        )

    async def find_by_email(self, email: str) -> dict | None:
        return await self._find_raw({"email": email})

    async def find_by_student_id(self, student_id: str) -> dict | None:
        """Usado por src/worker.py para resolver a qué `whatsapp_number` mandarle
        el aviso de un pedido (`orders.student_id` -> `users.student_id`, único
        por índice). `orders.student_id` no está
        validado contra esta colección al crear el pedido, así que puede no
        encontrar nada -- quien llame debe manejar el `None`."""
        return await self._find_raw({"student_id": student_id})

    async def touch_last_interaction(self, id_str: str) -> None:
        """Actualiza `last_interaction_at` (sostiene el TTL de inactividad, ver src/models/user.py)."""
        await self.update_one(id_str, {"last_interaction_at": datetime.now(timezone.utc)})
