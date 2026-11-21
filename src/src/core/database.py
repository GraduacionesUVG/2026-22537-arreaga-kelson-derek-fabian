"""
Conexión a MongoDB usando Motor (driver asíncrono oficial).

Reemplaza al `MockDatabase` de la plantilla original (pensada para SQL) por
un wrapper delgado sobre `AsyncIOMotorClient`. El patrón de uso es el mismo:

1. `mongo.connect()` se llama una vez al arrancar la app (ver lifespan en main.py).
2. Los repositorios NO abren su propia conexión: reciben una
   `AsyncIOMotorDatabase` ya conectada vía la dependencia `get_db`.
3. `mongo.close()` se llama al apagar la app.
"""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from src.configuration import settings


def redact_uri(uri: str) -> str:
    """La URI sin usuario ni contraseña, para logs: `mongodb+srv://***@cluster.x.mongodb.net/...`."""
    scheme, sep, rest = uri.partition("://")
    if not sep:
        return uri
    hosts, slash, path = rest.partition("/")
    if "@" in hosts:
        hosts = "***@" + hosts.rsplit("@", 1)[1]
    return f"{scheme}://{hosts}{slash}{path}"


class MongoDatabase:
    """Wrapper delgado sobre el cliente de Motor."""

    def __init__(self) -> None:
        self.client: AsyncIOMotorClient | None = None
        self.db: AsyncIOMotorDatabase | None = None

    def connect(self) -> None:
        self.client = AsyncIOMotorClient(settings.mongo.uri, tz_aware=True)
        self.db = self.client[settings.mongo.db_name]
        print(f"[mongo] Conectado a {redact_uri(settings.mongo.uri)} (db='{settings.mongo.db_name}')")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            print("[mongo] Conexión cerrada.")

    @property
    def is_connected(self) -> bool:
        return self.client is not None


mongo = MongoDatabase()


def get_db() -> AsyncIOMotorDatabase:
    """Dependencia de FastAPI: entrega la base de datos ya conectada a los repositorios.

    Uso típico en src/api/v1/deps.py:
        def get_order_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> OrderService:
            return OrderService(OrderRepository(db))
    """
    if mongo.db is None:
        raise RuntimeError(
            "MongoDB no está conectado. Verifica que mongo.connect() se llame "
            "en el lifespan de main.py antes de recibir requests."
        )
    return mongo.db
