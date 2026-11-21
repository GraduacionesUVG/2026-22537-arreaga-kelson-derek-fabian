# tests/

Tests con `pytest`. Instala dependencias de desarrollo primero:

```bash
pip install -r requirements-dev.txt
pytest
```

## Convención

Un archivo `test_<dominio>.py` por dominio, espejando `src/api/v1/routes/<dominio>.py`. `test_health.py` es el ejemplo mínimo — no necesita MongoDB corriendo.

Para testear un dominio con base de datos real (ej. `orders`), lo más simple es levantar el `docker-compose.yml` (`docker compose up -d mongo`) y apuntar `MONGO_URI` a `mongodb://localhost:27017` antes de correr pytest. No hay todavía fixtures de base de datos de prueba (ej. mongomock o una base "test" que se limpie entre tests) — es el siguiente paso natural cuando se agreguen tests de `orders`.

## Scripts manuales (no son tests de pytest)

`send_test_email.py` no empieza con `test_` a propósito -- manda un correo real vía Resend, así que pytest no debe recolectarlo ni correrlo solo. Úsalo para probar el envío sin pasar por `POST /auth/register` (y sin la restricción de `@uvg.edu.gt`, para poder mandarlo a cualquier correo y así aislar si un problema de entrega es de tu configuración de Resend/DNS o del lado del servidor de destino):

```bash
uv run python -m tests.send_test_email destino@gmail.com
```
