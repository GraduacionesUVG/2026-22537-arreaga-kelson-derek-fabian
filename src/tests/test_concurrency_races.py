"""Pruebas de concurrencia: dos operaciones compiten por el mismo pedido y solo una puede ganar.

Cada prueba repite la carrera muchas veces (CONCURRENCY_ITERATIONS, 200 por defecto). En cada
repetición se crea un pedido nuevo y se lanzan las dos operaciones a la vez con asyncio.gather.
La mitad de las repeticiones (al azar) usa el modo "barrera": las dos operaciones leen el pedido,
se detienen justo antes de su escritura condicional y se sueltan juntas, que es el peor caso. La
otra mitad usa el modo "libre", con esperas aleatorias de 0 a 3 ms antes de cada lectura y
escritura. Ver RaceControl.

Carreras que se prueban:
  1. La estación acepta la solicitud mientras la tarea de fondo la rechaza por falta de respuesta.
  2. La estación acepta dos veces la misma solicitud (doble toque o dos pestañas).
  3. Llegan a la vez el webhook de Tilopay y el retorno del cliente que confirman el mismo pago.
  4. El pago se confirma mientras la tarea de fondo cancela el pedido porque venció el link.

En todas se comprueba que el estado final es uno solo, que se registra un único evento de
desenlace y que no queda un link de pago vivo para un pedido que no se va a cobrar.

Modos:
  - Por defecto: MongoDB en memoria (mongomock-motor). Las operaciones se intercalan solo en los
    puntos `await`, así que lo que se prueba es la lógica de las actualizaciones condicionales.
  - Con CONCURRENCY_MONGO_URI=mongodb://localhost:27017 se usa un MongoDB real (por ejemplo
    `docker run -d --rm -p 27017:27017 --name mongo-concurrencia mongo:7`). Ahí las dos
    escrituras llegan de verdad en paralelo al servidor por conexiones distintas del pool de Motor.
    Se crea una base temporal `gp_concurrency_<id>` que se borra al terminar.

Ejecutar:  uv run pytest tests/test_concurrency_races.py -v -s
"""

import asyncio
import logging
import os
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from src.core.database import mongo
from src.models.order import OrderPipelineStatus
from src.repositories.order_repo import OrderRepository
from src.services.order_service import OrderConflictError
from src.services.payment_accounts import PROVIDER_TILOPAY
from src.services.payment_expiry import build_order_service
from src.services.payment_service import PaymentLink, PaymentService

ITERATIONS = int(os.getenv("CONCURRENCY_ITERATIONS", "200"))
REAL_MONGO_URI = os.getenv("CONCURRENCY_MONGO_URI", "").strip()
MODE = "MongoDB real" if REAL_MONGO_URI else "mongomock (en memoria)"


# --- Preparación --------------------------------------------------------------------------------


def _template(client, seed, db):
    """Crea un pedido real por la API (mongomock) y devuelve los documentos que hay que copiar."""
    r = client.post(
        "/api/orders/",
        json={"student_id": "20231", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Café", "quantity": 1}]},
    )
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]

    async def _read():
        order = await db.orders.find_one({"_id": ObjectId(order_id)})
        await db.orders.delete_one({"_id": ObjectId(order_id)})
        await db.order_events.delete_many({})
        return {
            "order": order,
            "restaurants": await db.restaurants.find({}).to_list(None),
            "kds_users": await db.kds_users.find({}).to_list(None),
            "menu_items": await db.menu_items.find({}).to_list(None),
        }

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_read())
    finally:
        loop.close()


class RaceControl:
    """Decide cómo se intercalan las dos operaciones en cada repetición.

    - Modo "libre": espera aleatoria de 0 a 3 ms antes de cada lectura/escritura de pedidos y la
      tarea de fondo arranca con un desfase aleatorio.
    - Modo "barrera": cada operación se detiene justo antes de su escritura condicional hasta que
      la otra también llega a la suya (las dos ya leyeron el pedido en el mismo estado); entonces
      se sueltan juntas en orden aleatorio. Es el peor caso: dos escrituras sobre el mismo pedido
      al mismo tiempo. No depende de la resolución del reloj (en Windows `asyncio.sleep` redondea
      a unos 15 ms), así que las dos ramas de la carrera se prueban en cualquier máquina.
    """

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.barrier = False
        self._arrived = 0
        self._release: asyncio.Event | None = None

    def new_round(self) -> str:
        self.barrier = self.rng.random() < 0.5
        self._arrived = 0
        self._release = asyncio.Event()
        return "barrera" if self.barrier else "libre"

    async def before_read(self):
        if not self.barrier:
            await asyncio.sleep(self.rng.uniform(0, 0.003))

    async def before_write(self):
        if not self.barrier:
            await asyncio.sleep(self.rng.uniform(0, 0.003))
            return
        self._arrived += 1
        if self._arrived >= 2:
            self._release.set()
        try:
            # Si la otra operación no llega a escribir (por ejemplo, leyó el pedido ya cerrado), se sigue.
            await asyncio.wait_for(self._release.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
        for _ in range(self.rng.randint(0, 3)):  # orden de salida aleatorio
            await asyncio.sleep(0)


def _jitter(monkeypatch, rng: random.Random) -> RaceControl:
    """Instala el control de intercalado en las lecturas y escrituras del repositorio de pedidos."""
    control = RaceControl(rng)
    original_update, original_find = OrderRepository.update_where, OrderRepository.find_by_id

    async def update_where(self, id_str, conditions, data):
        await control.before_write()
        return await original_update(self, id_str, conditions, data)

    async def find_by_id(self, id_str):
        await control.before_read()
        return await original_find(self, id_str)

    monkeypatch.setattr(OrderRepository, "update_where", update_where)
    monkeypatch.setattr(OrderRepository, "find_by_id", find_by_id)
    return control


def _n(outcomes: Counter, label: str) -> int:
    """Veces que salió `label`, sumando los dos modos."""
    return sum(v for k, v in outcomes.items() if k.split(" | ")[0] == label)


class FakeTilopay:
    """Sustituye a Tilopay: crea links falsos con una demora aleatoria y anota cuáles se borran."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.created: list[str] = []
        self.discarded: list[str] = []
        self.paid = True

    async def request_payment_link(self, order):
        await asyncio.sleep(self.rng.uniform(0, 0.005))  # la llamada a Tilopay tarda
        link_id = f"L-{uuid.uuid4().hex[:8]}"
        self.created.append(link_id)
        return PaymentLink(url=f"https://tilopay.test/{link_id}", provider=PROVIDER_TILOPAY,
                           link_id=link_id, account_user="api-user")

    async def discard_link(self, order):
        if order.get("payment_provider") == PROVIDER_TILOPAY and order.get("payment_link_id"):
            self.discarded.append(order["payment_link_id"])

    async def is_paid(self, order):
        await asyncio.sleep(self.rng.uniform(0, 0.003))
        return self.paid


async def _after(control: RaceControl, max_seconds: float, coro):
    """En modo libre arranca `coro` tras una espera aleatoria: la tarea de fondo hace una sola
    escritura y la otra operación varias, y sin este desfase casi siempre ganaría la tarea. En modo
    barrera arrancan juntas (la barrera se encarga de que choquen)."""
    if not control.barrier:
        await asyncio.sleep(control.rng.uniform(0, max_seconds))
    return await coro


def _install_fake_tilopay(monkeypatch, fake: FakeTilopay):
    monkeypatch.setattr(PaymentService, "request_payment_link", lambda self, order: fake.request_payment_link(order))
    monkeypatch.setattr(PaymentService, "discard_link", lambda self, order: fake.discard_link(order))
    monkeypatch.setattr(PaymentService, "is_paid", lambda self, order: fake.is_paid(order))


async def _open_db(memory_db):
    """Devuelve (db, cerrar) según el modo."""
    if not REAL_MONGO_URI:
        return memory_db, None
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(REAL_MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
    name = f"gp_concurrency_{uuid.uuid4().hex[:8]}"
    database = client[name]

    async def close():
        await client.drop_database(name)
        client.close()

    return database, close


async def _copy_catalog(db, template):
    if not REAL_MONGO_URI:
        return  # en memoria ya están
    for collection in ("restaurants", "kds_users", "menu_items"):
        if template[collection]:
            await db[collection].insert_many([dict(d) for d in template[collection]])


async def _new_order(db, template, **fields) -> dict:
    doc = dict(template["order"])
    doc["_id"] = ObjectId()
    doc["created_at"] = datetime.now(timezone.utc) - timedelta(minutes=30)
    doc.update(fields)
    await db.orders.insert_one(doc)
    return doc


async def _events(db, order_id: str) -> Counter:
    return Counter([e["event"] async for e in db.order_events.find({"order_id": order_id})])


def _kds_actor(order: dict) -> dict:
    return {"type": "kds", "id": order["offered_kds_id"], "restaurant_id": order["restaurant_id"], "username": "gh-uvg"}


def _run_race(memory_db, template, one_iteration):
    """Ejecuta `one_iteration(db, i)` ITERATIONS veces en un mismo bucle y devuelve los desenlaces."""

    async def main():
        db, close = await _open_db(memory_db)
        previous = mongo.db
        mongo.db = db  # el registro de eventos usa mongo.db
        try:
            await _copy_catalog(db, template)
            return Counter([await one_iteration(db, i) for i in range(ITERATIONS)])
        finally:
            mongo.db = previous
            if close:
                await close()

    loop = asyncio.new_event_loop()
    try:
        outcomes = loop.run_until_complete(main())
    finally:
        loop.close()
    print(f"\n[{MODE}] {ITERATIONS} repeticiones:")
    for label, count in sorted(outcomes.items()):
        print(f"    {count:>5}  {label}")
    return outcomes


# --- 1. Aceptar vs. rechazo automático por falta de respuesta ------------------------------------


def test_aceptar_contra_rechazo_por_falta_de_respuesta(client, seed, db, monkeypatch):
    template = _template(client, seed, db)
    rng = random.Random(1)
    control = _jitter(monkeypatch, rng)
    fake = FakeTilopay(rng)
    _install_fake_tilopay(monkeypatch, fake)

    async def iteration(db, i):
        mode = control.new_round()
        return f"{await _one(db)} | {mode}"

    async def _one(db):
        order = await _new_order(db, template)
        oid = str(order["_id"])
        service = build_order_service(db)
        fake.created.clear(), fake.discarded.clear()

        accept, expired = await asyncio.gather(
            service.update_status(oid, OrderPipelineStatus.ACCEPTED, actor=_kds_actor(order)),
            _after(control, 0.012, service.expire_unanswered_requests()),
            return_exceptions=True,
        )
        final = await db.orders.find_one({"_id": order["_id"]})
        events = await _events(db, oid)

        if isinstance(expired, Exception):
            raise expired
        if final["pipeline_status"] == "accepted":
            assert isinstance(accept, dict) and expired == 0
            assert events["order_accepted"] == 1 and events["payment_link_sent"] == 1
            assert events["order_rejected"] == 0
            assert final["payment_link_id"] in fake.created and fake.discarded == []
            return "ganó la aceptación"

        assert final["pipeline_status"] == "rejected" and final["cancel_reason"] == "no_response"
        assert expired == 1
        assert events["order_rejected"] == 1 and events["order_accepted"] == 0 and events["payment_link_sent"] == 0
        assert final.get("payment_link_id") is None
        if fake.created:
            # La estación pidió el link a Tilopay y perdió la carrera al escribir: el link se borra.
            assert isinstance(accept, OrderConflictError)
            assert fake.discarded == fake.created
            return "ganó el rechazo; la aceptación ya tenía link (borrado)"
        assert isinstance(accept, ValueError)  # leyó el pedido ya rechazado: no llegó a pedir link
        return "ganó el rechazo antes de pedir link"

    outcomes = _run_race(db, template, iteration)
    assert _n(outcomes, "ganó la aceptación") > 0
    assert _n(outcomes, "ganó el rechazo; la aceptación ya tenía link (borrado)") > 0


# --- 2. Doble aceptación de la misma solicitud ---------------------------------------------------


def test_doble_aceptacion_de_la_misma_solicitud(client, seed, db, monkeypatch):
    template = _template(client, seed, db)
    rng = random.Random(2)
    control = _jitter(monkeypatch, rng)
    fake = FakeTilopay(rng)
    _install_fake_tilopay(monkeypatch, fake)

    async def iteration(db, i):
        mode = control.new_round()
        return f"{await _one(db)} | {mode}"

    async def _one(db):
        order = await _new_order(db, template, created_at=datetime.now(timezone.utc))
        oid = str(order["_id"])
        service = build_order_service(db)
        fake.created.clear(), fake.discarded.clear()
        actor = _kds_actor(order)

        results = await asyncio.gather(
            service.update_status(oid, OrderPipelineStatus.ACCEPTED, actor=actor),
            service.update_status(oid, OrderPipelineStatus.ACCEPTED, actor=actor),
            return_exceptions=True,
        )
        final = await db.orders.find_one({"_id": order["_id"]})
        events = await _events(db, oid)

        winners = [r for r in results if isinstance(r, dict)]
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(winners) == 1 and len(losers) == 1
        assert isinstance(losers[0], (OrderConflictError, ValueError))
        assert final["pipeline_status"] == "accepted"
        assert events["order_accepted"] == 1 and events["payment_link_sent"] == 1
        # Queda vivo solo el link guardado en el pedido; cualquier otro se borró.
        assert set(fake.created) - set(fake.discarded) == {final["payment_link_id"]}
        return "las dos pidieron link" if len(fake.created) == 2 else "la segunda llegó tarde"

    outcomes = _run_race(db, template, iteration)
    assert _n(outcomes, "las dos pidieron link") > 0


# --- 3. Webhook y retorno del cliente confirman el mismo pago -------------------------------------


def _accepted_unpaid(minutes_to_expire: float) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "pipeline_status": "accepted",
        "payment_status": "pending",
        "payment_provider": PROVIDER_TILOPAY,
        "payment_link_id": f"L-{uuid.uuid4().hex[:8]}",
        "payment_account_user": "api-user",
        "accepted_at": now - timedelta(minutes=5),
        "payment_expires_at": now + timedelta(minutes=minutes_to_expire),
    }


def test_doble_confirmacion_del_mismo_pago(client, seed, db, monkeypatch):
    template = _template(client, seed, db)
    rng = random.Random(3)
    control = _jitter(monkeypatch, rng)
    fake = FakeTilopay(rng)
    _install_fake_tilopay(monkeypatch, fake)
    numbers: list[int] = []

    async def iteration(db, i):
        mode = control.new_round()
        return f"{await _one(db)} | {mode}"

    async def _one(db):
        order = await _new_order(db, template, **_accepted_unpaid(10))
        oid = str(order["_id"])
        service = build_order_service(db)

        results = await asyncio.gather(service.confirm_gateway_payment(oid), service.confirm_gateway_payment(oid))
        final = await db.orders.find_one({"_id": order["_id"]})
        events = await _events(db, oid)

        assert sorted(results) == ["already_paid", "paid"]
        assert final["payment_status"] == "paid" and final["otp_code"]
        assert events["order_paid"] == 1
        numbers.append(final["order_number"])
        return "un solo pago registrado"

    outcomes = _run_race(db, template, iteration)
    assert _n(outcomes, "un solo pago registrado") == ITERATIONS
    assert len(numbers) == len(set(numbers)), "dos pedidos recibieron el mismo número"
    # El que pierde pudo haber pedido ya su número al contador: puede haber saltos, nunca repetidos.
    print(f"Números de orden: {len(numbers)} únicos; saltos en la secuencia: {max(numbers) - min(numbers) + 1 - len(numbers)}")


# --- 4. Pago contra vencimiento del link -----------------------------------------------------------


def test_pago_contra_vencimiento_del_link(client, seed, db, monkeypatch):
    # Cuando gana el vencimiento, el servicio registra "PAGO RECIBIDO PARA UN PEDIDO CANCELADO" (es
    # lo esperado en esa rama); se silencia para que no tape el resumen.
    monkeypatch.setattr(logging.getLogger("orders"), "disabled", True)
    template = _template(client, seed, db)
    rng = random.Random(4)
    control = _jitter(monkeypatch, rng)
    fake = FakeTilopay(rng)
    _install_fake_tilopay(monkeypatch, fake)

    async def iteration(db, i):
        mode = control.new_round()
        return f"{await _one(db)} | {mode}"

    async def _one(db):
        order = await _new_order(db, template, **_accepted_unpaid(-1))  # el link venció hace 1 min
        oid = str(order["_id"])
        service = build_order_service(db)
        fake.discarded.clear()

        # La tarea de vencimiento ya consultó a Tilopay (sin pago) y va a cancelar; en ese instante
        # llega el webhook de un pago hecho en el último segundo.
        confirm, cancelled = await asyncio.gather(
            service.confirm_gateway_payment(oid),
            _after(control, 0.015, service.cancel_for_nonpayment(oid, expired=True)),
        )
        final = await db.orders.find_one({"_id": order["_id"]})
        events = await _events(db, oid)

        if final["payment_status"] == "paid":
            assert confirm == "paid" and cancelled is None
            assert events["order_paid"] == 1 and events["payment_link_expired"] == 0
            assert fake.discarded == []
            return "ganó el pago"

        assert final["pipeline_status"] == "cancelled" and final["cancel_reason"] == "payment_link_expired"
        assert cancelled is not None and confirm == "late"
        assert final.get("late_payment_at") is not None  # queda marcado para reembolso manual
        assert events["payment_link_expired"] == 1 and events["order_paid"] == 0
        assert fake.discarded == [final["payment_link_id"]]
        return "ganó el vencimiento (pago tardío marcado)"

    outcomes = _run_race(db, template, iteration)
    assert _n(outcomes, "ganó el pago") > 0
    assert _n(outcomes, "ganó el vencimiento (pago tardío marcado)") > 0

