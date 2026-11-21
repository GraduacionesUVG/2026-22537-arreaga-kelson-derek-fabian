# Imagen del servicio de WhatsApp (agent/webhook.py), separada del backend
# principal (Dockerfile en la raíz): son dos servicios distintos porque
# agent/ corre como proceso propio. Build context =
# esta carpeta (agent/), no la raíz del repo.

FROM python:3.11-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 3000

# Render inyecta $PORT; localmente (docker run suelto, sin PORT) cae a 3000,
# el mismo puerto que ya usaba `uvicorn webhook:app --port 3000` en local.
CMD uvicorn webhook:app --host 0.0.0.0 --port ${PORT:-3000} --no-server-header
