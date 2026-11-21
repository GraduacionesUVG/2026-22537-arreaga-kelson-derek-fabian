# repositories/

Única capa que habla directamente con MongoDB (vía Motor). Los servicios (`src/services/`) nunca deberían importar `motor` ni construir queries de Mongo directamente — siempre pasan por un repositorio.

## Patrón

`base_repo.py` define `BaseRepository` con el CRUD genérico (`find_by_id`, `find_many`, `insert_one`, `update_one`, `delete_one`) que ya convierte `_id` (ObjectId) a `id` (str) vía `serialize_document`.

Cada repositorio de dominio hereda de `BaseRepository` y solo agrega:

1. `collection_name` (importado desde `src/models/<dominio>.py::COLLECTION_NAME`).
2. Queries específicas que el CRUD genérico no cubre (ver `order_repo.py::find_active`, `update_status`, `set_otp` como referencia).

```python
class OrderRepository(BaseRepository):
    collection_name = COLLECTION_NAME

    async def find_active(self) -> list[dict]:
        return await self.find_many({"status": {"$in": [...]}})
```

## Cómo se instancia

Nunca directamente en el router. Se arma en `src/api/v1/deps.py`:

```python
def get_order_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> OrderService:
    return OrderService(OrderRepository(db))
```

## Placeholders pendientes

`menu_repo.py` y `restaurant_repo.py` solo tienen el CRUD genérico. Agrega métodos según los necesite el `service` correspondiente — no anticipes queries que nadie usa todavía.

`admin_user_repo.py` y `kds_user_repo.py` (autenticación) sí están completos: `find_by_email` (login de administración) y `find_by_username` (login de KDS, único a diferencia del `username` de `admin_users`) respectivamente. `user_repo.py` agrega `find_by_whatsapp_number`/`find_by_email`/`touch_last_interaction`, usados por el flujo de OTP.
