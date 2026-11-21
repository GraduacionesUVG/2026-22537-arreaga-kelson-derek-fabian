"""
Forma del documento de Mongo para la colección `users` — estudiantes
vinculados a su número de WhatsApp. A diferencia del diseño original, el
documento se crea desde el primer intento de registro (`email_verified=False`)
en vez de vivir aparte en una colección `pending_registrations` mientras no
se verifica: así "está registrado pero no ha verificado su correo" es un
estado consultable directamente aquí (ver `AuthService.check_registration`),
en vez de tener que mirar dos colecciones distintas.

Decisión: `email` es requerido y único. El flujo de verificación
(src/services/auth_service.py) depende de poder buscar "¿ya existe una
cuenta con este correo?" al detectar un cambio de número.

Decisión: `last_interaction_at` sostiene la política de inactividad (6
meses ≈ `INACTIVE_USER_TTL_DAYS`, ver .env.example): se actualiza en cada
mensaje autenticado y tiene un índice TTL (ver scripts/init_db.py). Cuando
pasan esos días sin interacción, Mongo borra el documento solo — el número
vuelve a quedar "no registrado" y quien escriba desde ahí (el mismo
estudiante u otra persona que herede el número) tiene que re-verificarse.
Como los pedidos se pagan por enlace de BAC, esto no permite pedidos
gratis; solo evita que alguien más "sea" el estudiante original en el chat.

Decisión: el número de WhatsApp NO se guarda en claro. `UserRepository`
(src/repositories/user_repo.py) lo cifra al escribir (`whatsapp_number_enc`,
Fernet con `USER_DATA_SECRET`) y guarda un HMAC para buscar e indexar
(`whatsapp_number_hash`); al leer lo devuelve descifrado como
`whatsapp_number`. El carné, el nombre y el correo quedan en claro: el carné
se deduce del correo institucional y es la llave hacia `orders`/`user_memory`.

Campos de verificación por OTP (reemplazan a `pending_registrations`):

- `email_verified`: `False` desde que se registra, `True` al confirmar el
  código OTP (`AuthService.verify_otp`). Mientras sea `False`, el bot debe
  contestar "correo institucional no verificado" en vez de dejar chatear
  (ver `AuthService.check_registration`).
- `otp_code_hash` / `otp_expires_at`: SOLO se guarda el hash SHA-256 del
  código de 6 dígitos (`src/core/security.py::hash_otp_code`), nunca el
  código en texto plano — mismo patrón que un token de reset de contraseña.
- `otp_attempts`: intentos fallidos consecutivos contra el código vigente;
  al llegar a `MAX_OTP_ATTEMPTS` el código queda invalidado y hay que pedir
  uno nuevo (protege contra fuerza bruta sobre un código de solo 6 dígitos).
- `otp_request_count` / `otp_window_started_at`: rate limit de reenvíos
  (`MAX_VERIFICATION_REQUESTS` por `VERIFICATION_REQUEST_WINDOW_HOURS`),
  evaluado en `AuthService.register` -- ya no depende de un índice TTL para
  "resetear" la ventana, se compara la fecha a mano.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

COLLECTION_NAME = "users"


@dataclass
class UserDocument:
    """
    Ejemplo de documento (estudiante que ya verificó su correo):

    {
        "_id": ObjectId("..."),
        "student_id": "5024-carne-demo",
        "full_name": "Nombre Apellido",
        "whatsapp_number_enc": "gAAAAABn...",   # cifrado (src/core/pii.py)
        "whatsapp_number_hash": "3f9a1c...",    # HMAC para buscar por número
        "email": "nombre@uvg.edu.gt",
        "email_verified": true,
        "otp_code_hash": null,
        "otp_expires_at": null,
        "otp_attempts": 0,
        "otp_request_count": 1,
        "otp_window_started_at": ISODate("2026-09-08T10:00:00Z"),
        "created_at": ISODate("2026-09-08T10:00:00Z"),
        "last_interaction_at": ISODate("2026-09-08T10:05:00Z")
    }
    """

    student_id: str
    full_name: str
    whatsapp_number: str  # en Mongo: whatsapp_number_enc + whatsapp_number_hash
    email: str
    email_verified: bool = False
    otp_code_hash: str | None = None
    otp_expires_at: datetime | None = None
    otp_attempts: int = 0
    otp_request_count: int = 0
    otp_window_started_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_interaction_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
