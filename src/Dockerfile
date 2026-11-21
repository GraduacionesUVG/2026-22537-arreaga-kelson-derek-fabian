FROM python:3.11-slim

WORKDIR /code

# Dependencias del sistema minimas para compilar paquetes con extensiones nativas
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Se copian tanto src/ (backend estructurado) como agent/ (orquestador LLM):
# src/services/agent_bridge.py importa modulos de agent/ en tiempo de
# ejecucion (ver ese archivo para el detalle de la integracion). scripts/
# trae las herramientas de Atlas (crear indices, usuarios de prueba).
COPY main.py .
COPY src/ ./src/
COPY agent/ ./agent/
COPY scripts/ ./scripts/

ENV PATH="/code/.venv/bin:$PATH"

EXPOSE 8000

# Forma shell (no exec) para que ${PORT} se expanda: Render (y Cloud Run) inyectan
# su propio puerto vía la variable de entorno PORT; localmente (sin esa variable)
# cae al 8000 de siempre. docker-compose.yml sigue mandando porque define su
# propio `command:`, que reemplaza este CMD por completo.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --no-server-header
