# Demostración del flujo de un pedido

Página que ejecuta, paso a paso, un pedido completo contra la API: desde que el estudiante escribe por WhatsApp hasta que recoge su orden en el mostrador. Mientras avanza muestra:

- **Izquierda:** cada llamada a la API, con su request y su response reales.
- **Arriba:** el estado del pedido (Solicitud → Aceptada → Pagada → En cocina → Lista → Entregada) con la hora de cada cambio.
- **Centro:** lo que ve el estudiante en WhatsApp, incluidas las notificaciones automáticas.
- **Derecha:** la pantalla de cocina (KDS) y el log de eventos, que llegan en vivo por WebSocket.

`demo_server.py` sirve la página y reenvía las llamadas al backend. Así se evita CORS y la llave interna del agente (`INTERNAL_API_KEY`) nunca llega al navegador.

## Uso rápido (sin base de datos ni llaves externas)

Desde `src/`:

```bash
# Terminal 1: la API real con un restaurante de prueba en memoria
uv run python demo/dev_backend.py

# Terminal 2: la página de la demostración (se abre en http://localhost:8765)
INTERNAL_API_KEY=ensayo-demo uv run python demo/demo_server.py --target http://localhost:8000
```

En PowerShell, la terminal 2 es: `$env:INTERNAL_API_KEY="ensayo-demo"; uv run python demo/demo_server.py --target http://localhost:8000`

En **⚙ Configurar** ingresa:

| Campo | Valor |
|---|---|
| Correo / contraseña del admin | `admin@localdeprueba.com` / `Demo2026!` |
| Usuario / PIN del KDS | `lp-cocina` / `2468` |
| Correo / contraseña Dev/TI (opcional) | `ops@qorder.demo` / `OpsDemo2026!` |

Los demás campos son opcionales. Todo vive en memoria y se borra al cerrar la terminal 1.

## Uso contra un backend desplegado

1. Crear el restaurante de prueba en la base de ese backend (usa `MONGO_URI` / `MONGO_DB_NAME` del `.env` y pide confirmación):

   ```bash
   uv run python -m scripts.seed_demo_restaurant
   ```

   Crea un restaurante abierto todos los días de 00:00 a 23:59, su admin, una estación KDS y un menú. Imprime las credenciales al final. Con `--ops-email` y `--ops-password` crea también un usuario Dev/TI.

2. Levantar la página apuntando al backend:

   ```bash
   INTERNAL_API_KEY=<llave del backend> uv run python demo/demo_server.py --target https://<url-del-backend>
   ```

El backend necesita `SIMULATED_PAYMENTS_ENABLED=true`: el pago se confirma con el webhook simulado `/webhooks/bac-payment`.

## Controles

- **▶ Reproducir flujo** (barra espaciadora): corre todo el pedido con la pausa elegida.
- **⏭ Siguiente** (tecla →): avanza un paso a la vez.
- **▶ fase:** corre una sola fase. Las marcadas con `+` (rechazo de cocina, pago fallido, seguridad, cierre de turno, alta de restaurante, agente LLM) no entran en la reproducción automática.
- **⟲ Nueva demo:** limpia todo para otra corrida.

---

## Guion del video (4 min)

Grabar a 1920×1080 con el navegador en pantalla completa (F11), con narración. Antes de grabar, ejecutar un paso y luego **⟲ Nueva demo** para que la API esté despierta. Avanzar con **→** mientras se habla; no hace falta leer los endpoints, la pantalla los muestra.

| Tiempo | En pantalla | Qué decir |
|---|---|---|
| 0:00-0:30 | Pantalla inicial | "QOrder permite pedir comida en el campus por WhatsApp, sin hacer cola. Este es el backend: la API que conecta al estudiante, la cocina y el restaurante. Voy a seguir un pedido de principio a fin. A la izquierda se ve cada llamada a la API; al centro, el WhatsApp del estudiante; a la derecha, la pantalla de cocina y el log de eventos en tiempo real." |
| 0:30-0:55 | Fase 0 (pasos 1-7) | "Primero se conectan el administrador y la cocina. La cocina abre su turno y desde ese momento puede recibir pedidos." |
| 0:55-1:45 | Fase 1 hasta crear la solicitud | "El estudiante pregunta qué hay para comer y el agente le muestra el menú. Antes de confirmar, el sistema calcula el total y la hora estimada según la cola real de la cocina. Al confirmar se crea la solicitud, y aparece sola en la pantalla de cocina." |
| 1:45-2:35 | Fase 2 (aceptar y pagar) | "La cocina acepta y al estudiante le llega el link de pago con una hora límite; arriba corre la cuenta regresiva. Aquí el pago está simulado: en producción la pasarela avisa y el backend verifica el pago con ella antes de marcarlo como pagado. Al confirmarse, la solicitud se convierte en orden y el estudiante recibe su código de retiro." |
| 2:35-3:15 | Fase 3 (preparar y entregar) | "La cocina la prepara y la marca lista, y el estudiante recibe el aviso para pasar a recogerla. En el mostrador, un código equivocado se rechaza y queda registrado; con el correcto, la orden se entrega y sale de la cola de cocina." |
| 3:15-3:45 | Línea de tiempo (paso 33) y KPIs (paso 34 o 36) | "El administrador puede ver la historia completa del pedido, con quién hizo cada cosa y a qué hora, y los indicadores del día." |
| 3:45-4:10 | Panel Dev/TI (pasos 37-45, rápido) | "El equipo técnico tiene su propio panel: todos los restaurantes, el estado de las integraciones y cuánto tardan las conversaciones con el agente." |
| 4:10-4:30 | Tracker en *Entregada* | "Así funciona el backend de QOrder: cada paso del pedido queda trazado y llega en tiempo real a quien lo necesita. El código, las instrucciones y el informe están en este repositorio." |

**Requisitos del video:** MP4 (H.264), entre 3 y 5 minutos, al menos 720p, menos de 100 MB, con audio claro. Guardarlo como `/demo/demo.mp4` en la raíz del repositorio. Si pesa más de 100 MB:

```bash
ffmpeg -i grabacion.mp4 -vf scale=1920:-2 -c:v libx264 -preset veryfast -crf 28 -c:a aac -b:a 128k demo.mp4
```
