"""
Punto de entrada de prueba: simula una conversación de WhatsApp por consola,

Uso:
    python main.py

Antes de cada turno se llama a POST /auth/whatsapp/check (igual que haría
agent/webhook.py con un mensaje real de Meta) para decidir si se le pasa el
mensaje al LLM o se contesta directo con el estado del registro. No hay integración real con WhatsApp todavía;
esto solo simula ese chequeo con un wa_id fijo.
"""
import os
import sys
import time

import httpx

from backend_client import BackendError, check_whatsapp_registration
from orchestator import get_session, split_into_whatsapp_messages

# La consola de Windows suele usar cp1252, que no puede imprimir emojis;
# el agente los usa en sus respuestas (ver SYSTEM_PROMPT), así que forzamos UTF-8.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# wa_id tal como lo mandaría Meta (sin "+"). Default: número de prueba ya
# verificado en el backend. Para probar los otros estados (not_registered /
# pending_verification), correr con SIMULATOR_WA_ID=<otro número> python main.py
SIMULATOR_WA_ID = os.getenv("SIMULATOR_WA_ID", "50239058318")


def main():
    print("=== Simulador de conversación ===")
    print(f"[simulador] wa_id de prueba: {SIMULATOR_WA_ID}\n")

    while True:
        try:
            user_input = input("Estudiante: ")
        except KeyboardInterrupt:
            print("\nFin de la conversación.")
            break

        try:
            check = check_whatsapp_registration(SIMULATOR_WA_ID)
        except BackendError as exc:
            print(f"[simulador] No se pudo consultar el registro: {exc.detail}\n")
            continue
        except httpx.HTTPError as exc:
            # El backend está en el plan free de Render: si estaba dormido,
            # el primer request lo despierta pero puede tardar/fallar por
            # timeout. No se pierde el turno del LLM por esto -- solo se le
            # pide al estudiante que reintente.
            print(f"[simulador] El backend no respondió a tiempo ({exc}). Puede estar 'despertando' (Render free) -- intenta de nuevo en unos segundos.\n")
            continue

        if check["status"] != "verified":
            respuesta = check["message"]
            login_url = check.get("login_url")
            if login_url:
                # El login_url que devuelve el backend es fijo (mismo link para
                # cualquier estudiante) -- se le pega el wa_id como query param
                # para que la página de registro lo lea y precargue el número,
                # sin que el estudiante tenga que escribirlo a mano. Esto es
                # cosa del bot/frontend, no cambia nada del backend.
                separator = "&" if "?" in login_url else "?"
                login_url = f"{login_url}{separator}wa={SIMULATOR_WA_ID}"
                respuesta += f"\n{login_url}"
            print(f"Agente: {respuesta}\n")
            continue

        # Igual que webhook.py: la sesión (y su memoria en Mongo) es por
        # estudiante; se arma desde Mongo la primera vez o si quedó inactiva.
        session = get_session(check["student_id"], check.get("full_name"))
        session.add_user_message(user_input)
        respuesta = session.run_turn()

        # Igual que en webhook.py: por default toda la respuesta es un solo
        # mensaje (cada mensaje de WhatsApp se cobra), tope en
        # MAX_MESSAGES_PER_REPLY.
        mensajes = split_into_whatsapp_messages(respuesta) or [respuesta]
        for i, mensaje in enumerate(mensajes):
            if i > 0:
                time.sleep(0.6)
            print(f"Agente: {mensaje}")
        print(f"[WhatsApp -> {SIMULATOR_WA_ID}] {len(mensajes)} mensaje(s) enviados\n")


if __name__ == "__main__":
    main()
