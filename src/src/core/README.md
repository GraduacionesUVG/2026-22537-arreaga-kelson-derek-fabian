# core/

Piezas transversales que no pertenecen a un dominio específico (orders, menus, etc.).

| Archivo | Responsabilidad |
|---|---|
| `database.py` | Conexión a MongoDB (Motor). Expone `mongo` (para `connect()`/`close()` en el lifespan de `main.py`) y `get_db` (dependencia de FastAPI usada por `src/api/v1/deps.py`). |
| `security.py` | JWT de sesión (admin/KDS), hashing de contraseñas/PIN y códigos OTP -- **implementado**. Solo `validate_entra_id_token` (SSO de Microsoft Entra ID, Módulo A) sigue sin implementar (`NotImplementedError`). |
| `realtime.py` | Difusión en memoria de eventos de pedidos hacia los WebSockets de KDS conectados a esta instancia (`KdsConnectionManager`/`kds_manager`). |

## Regla de oro

Nada en `src/repositories/` ni `src/services/` debería importar `motor` directamente para crear un cliente propio. Siempre reciben la base de datos ya conectada a través de `get_db` (inyección de dependencias de FastAPI), igual que la plantilla original recibía `session` en los repositorios SQL.
