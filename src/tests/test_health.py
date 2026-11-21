"""
Ejemplo mínimo de test de integración sobre el endpoint /health.

Correr con: pytest (requiere requirements-dev.txt instalado).

Nota: este test usa el TestClient de FastAPI, que ejecuta el lifespan de la
app (incluyendo mongo.connect()). Motor conecta de forma perezosa (no falla
si Mongo no responde todavía), así que este test no requiere Docker/Mongo
corriendo para pasar — solo valida la forma de la respuesta.
"""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health_check():
    response = client.get("/health/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert "database_connected" in body
