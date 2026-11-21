# services/

Capa de reglas de negocio. Un servicio recibe uno o más repositorios (y, si aplica, integraciones externas) y expone métodos con significado de negocio — nunca queries de Mongo crudas.

## Patrón (ver `order_service.py` como referencia completa)

```python
class OrderService:
    def __init__(self, repo: OrderRepository):
        self.repo = repo

    async def create_order(self, data: OrderCreate) -> dict:
        # reglas de negocio + arma el documento
        return await self.repo.insert_one(document)
```

El router nunca instancia el repositorio directamente; la cadena `repository -> service` se arma una sola vez en `src/api/v1/deps.py` y se inyecta con `Depends(...)`.

## Archivos

| Archivo | Estado | Depende de |
|---|---|---|
| `order_service.py` | **Implementado** (ejemplo de referencia) | `OrderRepository` |
| `menu_service.py` | Implementado (mínimo) | `MenuRepository` |
| `user_service.py` | Placeholder | `UserRepository` |
| `auth_service.py` | **Implementado**: verificación de WhatsApp por OTP de correo (`register`/`verify_otp`/`check_registration`) + login de administración (`admin_login`/`admin_change_password`) + login de KDS (`kds_pin_login`). El SSO Entra ID sigue sin implementar (`src/core/security.py::validate_entra_id_token`). | `UserRepository`, `AdminUserRepository`, `KdsUserRepository`, `src/integrations/email_client.py`, `src/core/security.py` |
| `payment_service.py` | Links de pago de Tilopay en la cuenta del restaurante del pedido (simulado si el restaurante no tiene cuenta, fuera de PRODUCTION) | `src/integrations/tilopay_client.py` |
| `payment_accounts.py` | Cuenta de Tilopay de cada restaurante: guardar (verificando con Tilopay), leer y borrar credenciales cifradas | `src/core/secret_box.py`, `src/integrations/tilopay_client.py` |
| `whatsapp_service.py` | **Implementado**: `send_text_message` delega en `src/integrations/whatsapp_client.py`, atrapando cualquier falla (nunca lanza) | `src/integrations/whatsapp_client.py` |
| `notification_templates.py` | **Implementado**: arma el texto de cada aviso de WhatsApp (aceptado+link de pago, pagado, listo, rechazado, pago fallido) | ninguno (funciones puras) |
| `agent_bridge.py` | **Puente a `agent/`** — leer su docstring, explica cómo se conecta sin modificar esa carpeta | módulos planos de `agent/` (import dinámico) |

Detalle completo del flujo de autenticación (`users`, `admin_users`, `kds_users`, rate limiting, TTLs). El contrato que usa el bot de `agent/` (`POST /auth/whatsapp/check`) está documentado aparte.

## Sobre `agent_bridge.py`

`agent/` contiene el orquestador LLM (function calling con OpenAI, transcripción con Whisper) que ya construyó otro integrante del equipo, con su propia estructura (no sigue el patrón de capas de `src/`). Por indicación explícita, ese código no se modifica.

`agent_bridge.py` es el único punto de esta carpeta que sabe que `agent/` existe: agrega su carpeta a `sys.path` e importa sus módulos en tiempo de ejecución. `src/api/v1/routes/webhooks.py` llama a `get_agent_response(user_id, texto)` sin saber nada de cómo está armado `agent/` por dentro. Si el día de mañana `agent/` se reescribe como paquete instalable, solo hay que actualizar este archivo.
