# models/

Describe la forma de cada documento **tal como vive en MongoDB**, una colección por archivo (`COLLECTION_NAME`).

## Por qué existe esto si Mongo es "schemaless"

Justamente porque Mongo no fuerza un esquema, es fácil que cada desarrollador guarde los documentos con una forma ligeramente distinta. Esta carpeta es la fuente de verdad **documentada** (no forzada en runtime) de cómo debería verse cada colección, para que:

- Los repositorios (`src/repositories/`) sepan qué `COLLECTION_NAME` usar.
- Cualquier persona nueva en el proyecto pueda ver un ejemplo de documento real sin tener que inspeccionar la base de datos.
- Si más adelante se decide migrar a un ODM con validación real (ej. [Beanie](https://beanie-odm.dev/)), estos dataclasses son el punto de partida.

## `models/` vs `schemas/`

| | `src/models/` | `src/schemas/` |
|---|---|---|
| Describe | El documento en MongoDB | El request/response de la API HTTP |
| Motor de validación | Ninguno (documentación + type hints) | Pydantic, se valida en cada request |
| Ejemplo | `OrderDocument` (incluye `_id`, `created_at`, etc.) | `OrderResponse` (incluye `id: str`, sin campos internos) |

Es normal que un mismo dominio (`order`) tenga un archivo en cada carpeta — no es duplicación, son dos capas distintas del mismo concepto.

## Colecciones de autenticación

Tres sistemas de login independientes:

- `user.py` (`users`): estudiantes por WhatsApp. Ya no hay una colección `pending_registrations` aparte -- el documento se crea desde el primer intento de registro con `email_verified=False` y los campos `otp_*` (código OTP de 6 dígitos, rate limiting), y pasa a `email_verified=True` al verificarse.
- `admin_user.py` (`admin_users`): login de administración/analítica, correo + contraseña + JWT.
- `kds_user.py` (`kds_users`): login del KDS (pantalla de cocina), usuario + PIN numérico + JWT -- separado de `admin_users` porque son secretos y flujos de UX distintos (ver su docstring).
