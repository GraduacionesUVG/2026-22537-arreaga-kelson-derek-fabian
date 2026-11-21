# schemas/

Modelos Pydantic que validan lo que **entra y sale por la API HTTP**. Cada router (`src/api/v1/routes/`) declara sus request bodies y `response_model` usando clases de aquí.

## Convención

Para cada dominio (`order`, `menu`, `user`, `restaurant`) se define:

- `<Dominio>Base`: campos comunes.
- `<Dominio>Create`: lo que el cliente envía al crear (hereda de `Base`).
- `<Dominio>Response`: lo que la API devuelve (hereda de `Base` + agrega `id: str`).

Ver `order.py` como referencia completa (incluye además `OrderStatusUpdate` y `OtpValidateRequest` para los otros endpoints del módulo de pedidos).

## Reglas

1. Nunca devuelvas un documento de Mongo directamente desde un router — siempre pasa por un schema de `Response` (por eso `_id` se convierte a `id: str` en `src/repositories/base_repo.py::serialize_document`).
2. La lógica de negocio (cálculos, validaciones cruzadas) va en `src/services/`, no aquí. Los schemas solo validan forma y tipos.
3. Si el mismo dominio necesita un modelo distinto para Mongo (con `created_at`, campos internos, etc.), ese vive en `src/models/`, no se mezcla aquí.

`auth.py` es la excepción a la convención `Base`/`Create`/`Response`: agrupa los schemas de dos flujos distintos (verificación de WhatsApp por correo y login KDS/admin) porque son requests/responses de acción, no de un recurso CRUD.
