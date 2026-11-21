"""
Puente entre quien necesita mandar un mensaje (src/worker.py) y el cliente
HTTP de Meta (src/integrations/whatsapp_client.py). Separado del cliente a
propósito: acá es donde iría, si hiciera falta, lógica de reintento/formato
que no es puramente "llamar a la Graph API".
"""

import logging

from src.integrations import whatsapp_client

logger = logging.getLogger("whatsapp_service")


class WhatsAppService:
    async def send_text_message(self, to: str, text: str) -> bool:
        """Nunca lanza -- quien llama (src/worker.py) ya asume que un envío de
        WhatsApp puede fallar (número inválido, token vencido, límite de la
        API) y que eso no debe tumbar el procesamiento del evento que lo disparó."""
        try:
            await whatsapp_client.send_text(to, text)
        except Exception:
            logger.exception("No se pudo enviar el mensaje de WhatsApp a %s.", to)
            return False
        return True

    async def send_template_message(
        self,
        to: str,
        name: str,
        language: str,
        body_params: list[str] | None = None,
        url_button_param: str | None = None,
    ) -> bool:
        """Igual que send_text_message: nunca lanza, devuelve si salió."""
        try:
            await whatsapp_client.send_template(to, name, language, body_params, url_button_param)
        except Exception:
            logger.exception("No se pudo enviar la plantilla '%s' de WhatsApp a %s.", name, to)
            return False
        return True
