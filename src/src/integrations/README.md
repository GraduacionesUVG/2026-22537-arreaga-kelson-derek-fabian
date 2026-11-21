# integrations/

Clientes HTTP "crudos" hacia servicios externos: Meta (WhatsApp Cloud API), Tilopay (links de pago) y Microsoft Entra ID (SSO).

## Por qué esta capa es distinta de `services/`

- `integrations/`: sabe **cómo** hablar con el proveedor externo (URLs, headers, formato del payload, firmas). No sabe nada del dominio del restaurante.
- `services/`: sabe **qué** mensaje mandar y **cuándo** (reglas de negocio). Llama a `integrations/` para el detalle de transporte.

Ejemplo: `whatsapp_service.send_order_ready_notification(to)` decide el texto del mensaje según el estado del pedido; `whatsapp_client.send_message(to, payload)` solo hace el POST HTTP a la Graph API de Meta.

## Estado actual

`whatsapp_client.py` **está implementado** (llama de verdad a la Send Message API de Meta) -- requiere `WHATSAPP_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID` configurados en `.env`; sin ellos, rechaza el envío con un error explícito en vez de intentar una llamada que fallaría igual. `tilopay_client.py` **está implementado** (login con token en memoria, crear/consultar/borrar links de pago); recibe las credenciales de la cuenta de cada restaurante (`TilopayCredentials`), que se guardan cifradas en `restaurants.payment_gateway`. `entra_id_client.py` sigue siendo un placeholder (`NotImplementedError`).

`events.py` es distinto de los demás: no habla con un proveedor externo de "dominio" sino con Kafka (opcional, `KAFKA_ENABLED`), para publicar y (si hace falta) reenviar los eventos de pedidos que llegan al WebSocket del KDS y a `src/worker.py` (avisos de WhatsApp).

`email_client.py` **sí está implementado** (API HTTP de Resend vía `httpx`, no SMTP -- los planes free de Render bloquean los puertos SMTP 25/465/587): lo usa `src/services/auth_service.py` para mandar el código OTP de verificación de WhatsApp (`send_otp_email`). Si `RESEND_API_KEY` está vacío en `.env` (default), no falla — imprime el correo en consola, para poder probar el flujo sin credenciales reales.
