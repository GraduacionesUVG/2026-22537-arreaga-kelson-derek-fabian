# QOrder: Backend y Business Intelligence

**Prototipo de un sistema basado en agentes conversacionales para la asistencia en la gestión de pedidos y colas virtuales en restaurantes de servicio rápido: módulo de Backend y Business Intelligence.**

Trabajo de graduación, Universidad del Valle de Guatemala (2026).

## Descripción

QOrder permite que los estudiantes pidan comida en los restaurantes del campus por WhatsApp, sin hacer cola. Un agente conversacional atiende al estudiante y este repositorio contiene la **API que sostiene todo el flujo**:

- **Registro y verificación:** el estudiante registra su número de WhatsApp con su correo institucional y lo verifica con un código OTP.
- **Rutas del agente:** restaurantes, menú, cotización con hora estimada según la cola real de la cocina, creación y consulta de pedidos, y memoria del estudiante.
- **Cocina (KDS):** recibe las solicitudes en tiempo real por WebSocket, maneja turnos y pausas, acepta o rechaza pedidos y los entrega validando el código del estudiante.
- **Cobro:** al aceptar, se genera un link de pago con hora límite (Tilopay, o pago simulado en ambientes de prueba). Al confirmarse el pago, la solicitud se convierte en orden.
- **Notificaciones:** los avisos de WhatsApp al estudiante (aceptado, pagado, listo, entregado) salen por eventos, con Kafka opcional.
- **Panel de administración:** catálogo y carga por Excel, estaciones, log de eventos en vivo, línea de tiempo de cada pedido, KPIs y resumen de turnos.
- **Panel Dev/TI (OPS):** todos los restaurantes y usuarios, salud de las integraciones (OpenAI, WhatsApp), KPIs globales y tiempos de conversación del agente.

El código está en capas dentro de `src/src/`: las rutas (`api/`) delegan en los servicios (`services/`), que aplican las reglas de negocio, y los repositorios (`repositories/`) son la única capa que habla con MongoDB. La carpeta `src/agent/` contiene el orquestador conversacional (OpenAI con function calling), que usa esta API como herramientas.

## Tecnologías utilizadas

- Python 3.11, FastAPI, Pydantic, Uvicorn
- MongoDB (Motor), MongoDB Atlas en producción
- WebSockets para el KDS y la consola de eventos
- Apache Kafka (opcional) para eventos y notificaciones
- JWT y bcrypt para la autenticación; cifrado de los números de teléfono
- Integraciones: WhatsApp Cloud API (Meta), OpenAI, Resend (correo), Tilopay (pagos)
- Docker y Docker Compose; despliegue en Render
- `uv` para dependencias y `pytest` para pruebas

## Requisitos previos

- [Docker](https://docs.docker.com/get-docker/) y Docker Compose, **o** Python 3.10+ con [`uv`](https://docs.astral.sh/uv/) y una instancia de MongoDB
- Git
- Opcional, para las integraciones reales: llaves de OpenAI, WhatsApp Cloud API y Resend

## Instalación

1. **Clonar el repositorio**

   ```bash
   git clone https://github.com/GraduacionesUVG/2026-22537-arreaga-kelson-derek-fabian.git
   cd 2026-22537-arreaga-kelson-derek-fabian/src
   ```

2. **Configurar las variables de entorno**

   ```bash
   cp .env.example .env
   ```

   Para correr localmente bastan los valores por defecto (`ENVIRONMENT=DEVELOPMENT`, MongoDB del `docker-compose`). Define también `INTERNAL_API_KEY` con cualquier texto: es la llave con la que el agente llama a la API.

3. **Instalar dependencias y ejecutar**

   Con Docker (recomendado):

   ```bash
   docker compose up --build
   ```

   O sin Docker, con una instancia de MongoDB accesible desde `MONGO_URI`:

   ```bash
   uv sync
   uv run uvicorn main:app --reload
   ```

   La API queda en `http://localhost:8000`; la documentación interactiva está en `http://localhost:8000/docs`.

4. **Cargar un restaurante de prueba** (restaurante abierto todo el día, admin, estación KDS y menú):

   ```bash
   uv run python -m scripts.seed_demo_restaurant --yes
   # con Docker: docker compose exec api python -m scripts.seed_demo_restaurant --yes
   ```

5. **Pruebas automatizadas** (usan MongoDB en memoria; no requieren Docker):

   ```bash
   uv run pytest
   ```

### Demostración del flujo de un pedido

`src/demo/` contiene una página que ejecuta paso a paso un pedido completo contra la API y muestra en vivo el teléfono del estudiante, la pantalla de cocina y el log de eventos. Para usarla sin base de datos ni llaves externas:

```bash
cd src
uv run python demo/dev_backend.py                                   # terminal 1: API con datos en memoria
INTERNAL_API_KEY=ensayo-demo uv run python demo/demo_server.py --target http://localhost:8000   # terminal 2
```

Instrucciones completas en [`src/demo/README.md`](src/demo/README.md).

## Capturas

Pantallas de la demostración del flujo de un pedido ([carpeta `/capturas`](capturas/)):

| # | Pantalla |
|---|---|
| [01](capturas/captura-01.png) | Pantalla inicial de la demostración |
| [02](capturas/captura-02.png) | Admin y cocina conectados; turno abierto |
| [03](capturas/captura-03.png) | El estudiante consulta el menú del día |
| [04](capturas/captura-04.png) | Solicitud creada; aparece en la pantalla de cocina |
| [05](capturas/captura-05.png) | La cocina acepta; link de pago con hora límite |
| [06](capturas/captura-06.png) | Pago confirmado; la solicitud se convierte en orden con código de retiro |
| [07](capturas/captura-07.png) | Orden en preparación |
| [08](capturas/captura-08.png) | Orden lista para recoger |
| [09](capturas/captura-09.png) | Código incorrecto en mostrador (rechazado y registrado) |
| [10](capturas/captura-10.png) | Orden entregada |
| [11](capturas/captura-11.png) | Línea de tiempo completa del pedido (panel de administración) |
| [12](capturas/captura-12.png) | KPIs del turno de la estación |
| [13](capturas/captura-13.png) | Panel Dev/TI: tiempos de conversación del agente |

## Demo

Video demostrativo: [`/demo/demo.mp4`](demo/demo.mp4)

## Documentación

- Informe final: [`/docs/informe_final.pdf`](docs/informe_final.pdf)
- Consentimiento de uso de foto y video: [`/docs/consentimiento_firmado.pdf`](docs/consentimiento_firmado.pdf)

## Autores

- **Derek Fabian Arreaga Kelson**, carné 22537. Módulo de Backend y Business Intelligence.

Asesor: Ing. Gabriel Brolo Tobar.

El orquestador conversacional (`src/agent/`) corresponde al módulo de agente conversacional del proyecto QOrder, desarrollado por otro integrante del equipo.

## Licencia

Distribuido bajo la licencia MIT. Ver [`LICENSE`](LICENSE).
