# src/ — Backend estructurado (FastAPI + MongoDB)

Arquitectura en capas, adaptada del template `my-fastapi-template` (que usaba SQL) para usar **MongoDB** vía **Motor** (driver asíncrono oficial).

## Flujo de una request (de afuera hacia adentro)

```
Cliente HTTP
   │
   ▼
src/api/v1/routes/<dominio>.py   (router: valida input con schemas, llama al service)
   │
   ▼
src/services/<dominio>_service.py (reglas de negocio, orquesta repos/integrations)
   │
   ▼
src/repositories/<dominio>_repo.py (única capa que arma queries de Mongo)
   │
   ▼
MongoDB (vía Motor, conexión gestionada por src/core/database.py)
```

La inyección de dependencias (`src/api/v1/deps.py`) arma la cadena `repository -> service` por request usando `Depends(...)` de FastAPI, igual que hacía la plantilla original con SQL.

## Ejemplo de referencia: Orders

El dominio **orders** (`src/models/order.py`, `src/schemas/order.py`, `src/repositories/order_repo.py`, `src/services/order_service.py`, `src/api/v1/routes/orders.py`) está completo y funcional contra MongoDB real. Úsalo como plantilla al crear un dominio nuevo — copia esos cinco archivos, cambia el nombre y ajusta los campos.

`menus` tiene una versión más pequeña del mismo patrón (útil si `orders` te resulta demasiado grande como primer ejemplo).

## Mapa de carpetas

| Carpeta | Qué contiene | README |
|---|---|---|
| `configuration/` | Settings (`pydantic-settings`) leídos de `.env` | [configuration/README.md](configuration/README.md) |
| `core/` | Conexión a Mongo, seguridad/JWT (placeholder) | [core/README.md](core/README.md) |
| `models/` | Forma de los documentos en MongoDB | [models/README.md](models/README.md) |
| `schemas/` | Validación de request/response de la API | [schemas/README.md](schemas/README.md) |
| `repositories/` | Única capa que habla con Mongo | [repositories/README.md](repositories/README.md) |
| `services/` | Reglas de negocio + puente hacia `agent/` | [services/README.md](services/README.md) |
| `integrations/` | Clientes HTTP hacia Meta, BAC, Entra ID | [integrations/README.md](integrations/README.md) |
| `api/` | Routers de FastAPI | [api/README.md](api/README.md) |

## Diferencia clave con la plantilla original

La plantilla (`my-fastapi-template`) usaba SQL con una sesión (`session`) genérica pasada a los repositorios. Aquí:

- `session` se reemplaza por una `AsyncIOMotorDatabase` (Mongo) inyectada vía `get_db` (`src/core/database.py`).
- Todos los repositorios/servicios son **async** (Motor es asíncrono).
- Se agrega `src/models/` porque en SQL el "esquema" lo definía la tabla; en Mongo (schemaless) hace falta documentarlo explícitamente.
- `_id` (ObjectId de Mongo) se convierte a `id: str` en `src/repositories/base_repo.py::serialize_document`, para que los schemas de respuesta no tengan que lidiar con `ObjectId`.

## Proceso aparte: `src/worker.py`

Además del proceso principal (`main.py`, la API), existe un segundo punto de
entrada independiente: `python -m src.worker` (servicio `worker` en
`docker-compose.yml`, perfil `kafka`). Consume los eventos de pedidos
publicados hacia Kafka y manda los avisos de WhatsApp correspondientes --
corre separado de la API a propósito, para que una llamada externa lenta
(Graph API de Meta) no comparta el mismo proceso ni arriesgue el tiempo de
respuesta de crear/aceptar/pagar un pedido.

## Nota sobre `agent/`

`agent/` (en la raíz del proyecto) contiene el orquestador LLM ya construido por otro integrante del equipo y **no se modifica**. Se conecta a este backend a través de `src/services/agent_bridge.py` — lee ese archivo para el detalle completo de la integración.
