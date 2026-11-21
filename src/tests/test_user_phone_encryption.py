"""El número de WhatsApp de `users` se guarda cifrado (src/core/pii.py, src/repositories/user_repo.py)."""

import asyncio

from src.core.pii import decrypt_value, whatsapp_number_hash
from src.repositories.user_repo import UserRepository, ensure_user_phone_indexes
from src.services.customer_notifications import resolve_whatsapp_number

NUMBER = "+50255551234"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _register(client, number=NUMBER, email="ram22718@uvg.edu.gt", student_id="22718"):
    return client.post(
        "/auth/register",
        json={"whatsapp_number": number, "full_name": "Ana Ramírez", "email": email, "student_id": student_id},
    )


def test_registro_guarda_el_numero_cifrado(client, db):
    assert _register(client).status_code == 200

    raw = _run(db.users.find_one({"student_id": "22718"}))
    assert "whatsapp_number" not in raw
    assert NUMBER not in str(raw)
    assert decrypt_value(raw["whatsapp_number_enc"]) == NUMBER
    assert raw["whatsapp_number_hash"] == whatsapp_number_hash(NUMBER)


def test_flujo_completo_con_numero_cifrado(client, db):
    otp = _register(client).json()["debug_otp_code"]
    assert client.post("/auth/verify-otp", json={"whatsapp_number": NUMBER, "otp_code": otp}).json()["status"] == "verified"

    check = client.post("/auth/whatsapp/check", json={"whatsapp_number": NUMBER}).json()
    assert check["status"] == "verified"
    assert check["student_id"] == "22718"

    # El aviso de un pedido obtiene el número en claro.
    assert _run(resolve_whatsapp_number(UserRepository(db), {"student_id": "22718"})) == NUMBER


def test_conflicto_de_correo_muestra_numero_enmascarado(client, db):
    otp = _register(client).json()["debug_otp_code"]
    client.post("/auth/verify-otp", json={"whatsapp_number": NUMBER, "otp_code": otp})

    resp = _register(client, number="+50299998888")
    assert resp.status_code == 409
    assert resp.json()["detail"]["existing_number_masked"].endswith("1234")


def test_documento_viejo_en_claro_se_lee_y_se_cifra(db):
    _run(db.users.insert_one({"student_id": "22075", "whatsapp_number": NUMBER, "email_verified": True}))
    repo = UserRepository(db)

    user = _run(repo.find_by_whatsapp_number(NUMBER))
    assert user["whatsapp_number"] == NUMBER
    assert "whatsapp_number_enc" not in user and "whatsapp_number_hash" not in user

    raw = _run(db.users.find_one({"student_id": "22075"}))
    assert "whatsapp_number" not in raw
    assert decrypt_value(raw["whatsapp_number_enc"]) == NUMBER
    # Ya cifrado, se sigue encontrando (por hash), también sin el '+'.
    assert _run(repo.find_by_whatsapp_number("50255551234"))["student_id"] == "22075"


def test_indices_reemplazan_el_unico_en_claro(db):
    async def scenario():
        await db.users.create_index("whatsapp_number", unique=True, name="uniq_whatsapp_number")
        await db.users.insert_one({"student_id": "1", "whatsapp_number": "+50211112222"})
        await db.users.insert_one({"student_id": "2", "whatsapp_number": "+50233334444"})
        await ensure_user_phone_indexes(db)
        # Ya sin el índice viejo, dos usuarios sin número en claro no chocan.
        repo = UserRepository(db)
        for student_id in ("1", "2"):
            await repo.find_by_student_id(student_id)
        await ensure_user_phone_indexes(db)  # idempotente
        return await db.users.index_information()

    indexes = _run(scenario())
    assert "uniq_whatsapp_number" not in indexes
    assert "uniq_whatsapp_number_hash" in indexes
