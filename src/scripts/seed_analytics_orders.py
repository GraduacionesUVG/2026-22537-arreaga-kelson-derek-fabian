"""
Puebla la base con pedidos históricos de Golden Harvest para tener datos
realistas en el dashboard (KPIs y gráficas).

- 50-80 pedidos por día entre semana y 20-35 los sábados (configurable);
  los días cerrados según el horario del restaurante no llevan pedidos.
- Afluencia por hora con pico al mediodía; el almuerzo es lo más vendido.
- Todos los pedidos los atiende una sola estación KDS.
- Resultados: ~89% entregados, ~6% abandonados (pago fallido o link vencido),
  ~3% rechazados y ~2% cancelados antes de aceptar.
- Tiempos reales de aceptación, pago, preparación y entrega, más el registro
  de eventos (`order_events`) para el log y la línea de tiempo.
- Estudiantes simulados con clientes frecuentes y ocasionales (recompra).

Los documentos generados llevan `seed_tag = "analytics_demo"`. Cada corrida
borra primero lo generado por la anterior (usa `--keep` para acumular) y
`--clear` solo borra.

    docker compose exec api python -m scripts.seed_analytics_orders
    docker compose exec api python -m scripts.seed_analytics_orders --days 45 --include-today
    docker compose exec api python -m scripts.seed_analytics_orders --kds-username gh-uvg
    docker compose exec api python -m scripts.seed_analytics_orders --clear

Requiere el restaurante y su menú (scripts/seed_menu_test_data.py) y al menos
un usuario KDS de Golden Harvest. Con ENVIRONMENT=PRODUCTION exige
`--allow-production`.
"""

import argparse
import asyncio
import random
import string
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient

from src.configuration import settings
from src.core.timeutils import GUATEMALA_TZ, local_today
from src.models.kds_user import normalize_menu_filter
from src.models.menu import resolve_schedule_type
from src.models.order_event import describe_event
from src.models.restaurant import WEEKDAYS, normalize_opening_hours
from src.repositories.counter_repo import SEQUENCE_START
from src.services.kpi_service import compute_kpis
from src.services.menu_service import is_offered_on

SEED_TAG = "analytics_demo"
RESTAURANT_NAME = "Golden Harvest"

LUNCH_CATEGORIES = {"almuerzos", "almuerzos del día"}
DRINK_CATEGORIES = {"bebidas"}
DINNER_CATEGORIES = {"cenas"}

# Peso relativo de pedidos por hora de apertura (se normaliza a las horas abiertas).
HOUR_WEIGHTS = {7: 3, 8: 5, 9: 6, 10: 6, 11: 12, 12: 24, 13: 20, 14: 8, 15: 5, 16: 6, 17: 6, 18: 5, 19: 3, 20: 1}
# Peso relativo de productos que no son almuerzo ni bebida (por nombre; el resto pesa 2).
ITEM_WEIGHTS = {"Hamburguesa": 6, "Baguette chapin": 5, "Sushi california roll": 4, "Sushi dragon roll": 3}

OUTCOMES = [("delivered", 0.89), ("abandoned", 0.06), ("rejected", 0.03), ("cancelled", 0.02)]


def _pick(rng: random.Random, weighted: list[tuple]):
    return rng.choices([w[0] for w in weighted], weights=[w[1] for w in weighted])[0]


def _utc(day: date, hour: int, minute: float) -> datetime:
    local = datetime.combine(day, time(0, 0), tzinfo=GUATEMALA_TZ) + timedelta(hours=hour, minutes=minute)
    return local.astimezone(timezone.utc)


def _otp(rng: random.Random) -> str:
    return "".join(rng.choices(string.digits, k=4))


class Generator:
    def __init__(self, rng: random.Random, restaurant: dict, menu: list[dict], station: dict, args):
        self.rng = rng
        self.restaurant = restaurant
        self.menu = menu
        self.station = station
        self.args = args
        self.hours = normalize_opening_hours(restaurant.get("opening_hours"))

    # --- Catálogo del día ------------------------------------------------------------

    def catalog_for(self, day: date) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        offered = [i for i in self.menu if is_offered_on(i, day) and i.get("available", True) and not i.get("dine_in_only")]
        cat = lambda i: (i.get("category") or "").strip().lower()
        lunches = [i for i in offered if cat(i) in LUNCH_CATEGORIES]
        drinks = [i for i in offered if cat(i) in DRINK_CATEGORIES]
        dinners = [i for i in offered if cat(i) in DINNER_CATEGORIES]
        others = [i for i in offered if i not in lunches and i not in drinks and i not in dinners]
        return lunches, drinks, dinners, others

    # --- Volumen y horario --------------------------------------------------------------

    def orders_for_day(self, day: date, index: int, total_days: int) -> int:
        if day.weekday() == 5:
            low, high = self.args.saturday_min, self.args.saturday_max
        else:
            low, high = self.args.min_per_day, self.args.max_per_day
        # Adopción: las primeras semanas se piden un poco menos que al final del periodo.
        progress = index / max(1, total_days - 1)
        center = low + (high - low) * (0.4 + 0.45 * progress)
        value = int(round(self.rng.gauss(center, (high - low) / 6)))
        if day.weekday() == 4:  # viernes un poco más flojo
            value = int(value * 0.92)
        return max(low, min(high, value))

    def open_hours(self, day: date) -> tuple[int, int] | None:
        hours = self.hours[WEEKDAYS[day.weekday()]]
        if hours["closed"]:
            return None
        open_h, open_m = map(int, hours["open"].split(":"))
        close_h, close_m = map(int, hours["close"].split(":"))
        return open_h * 60 + open_m, close_h * 60 + close_m

    def random_minute(self, window: tuple[int, int], limit: datetime | None, day: date) -> float | None:
        open_min, close_min = window
        close_for_orders = close_min - 20  # no se reciben pedidos en los últimos 20 minutos
        hours = [h for h in range(24) if open_min < (h + 1) * 60 and h * 60 < close_for_orders]
        weights = [HOUR_WEIGHTS.get(h, 1) for h in hours]
        for _ in range(20):
            hour = self.rng.choices(hours, weights=weights)[0]
            minute = hour * 60 + self.rng.uniform(0, 60)
            if not (open_min <= minute <= close_for_orders):
                continue
            if limit is not None and _utc(day, 0, minute) > limit:
                continue
            return minute
        return None

    # --- Pedido --------------------------------------------------------------------------

    def build_items(self, hour: int, catalog) -> list[tuple[dict, int]]:
        lunches, drinks, dinners, others = catalog
        rng = self.rng
        lines: list[tuple[dict, int]] = []
        lunch_probability = 0.72 if 11 <= hour <= 14 else (0.35 if hour in (9, 10, 15) else 0.12)
        if lunches and rng.random() < lunch_probability:
            lines.append((rng.choice(lunches), 2 if rng.random() < 0.08 else 1))
        else:
            pool = list(others)
            if dinners and hour >= 17:
                pool += dinners * 3
            if not pool:
                pool = lunches or drinks
            weights = [ITEM_WEIGHTS.get(i["name"], 2) for i in pool]
            main = rng.choices(pool, weights=weights)[0]
            lines.append((main, 2 if rng.random() < 0.1 else 1))
            if rng.random() < 0.18 and others:
                extra = rng.choice(others)
                if extra is not main:
                    lines.append((extra, 1))
        if drinks and rng.random() < 0.45:
            lines.append((rng.choice(drinks), 2 if rng.random() < 0.1 else 1))
        return lines

    def order_items(self, lines: list[tuple[dict, int]]) -> tuple[list[dict], float]:
        items, total = [], 0.0
        for menu_item, qty in lines:
            modifiers = []
            for modifier in menu_item.get("modifiers") or []:
                if self.rng.random() < 0.25:
                    modifiers.append({"name": modifier["name"], "price": float(modifier.get("price", 0)), "custom": False})
            unit = float(menu_item["price"])
            mods_total = sum(m["price"] for m in modifiers)
            line_total = round((unit + mods_total) * qty, 2)
            total += line_total
            items.append(
                {
                    "product_name": menu_item["name"],
                    "quantity": qty,
                    "modifiers": modifiers,
                    "notes": None,
                    "unit_price": unit,
                    "modifiers_total": round(mods_total, 2),
                    "line_total": line_total,
                    "prep_time_minutes": float(menu_item.get("prep_time_minutes", 5)),
                    "menu_item_id": menu_item["id"],
                    "category": menu_item.get("category"),
                    "ready_from": menu_item.get("ready_from"),
                }
            )
        return items, round(total, 2)

    def build_order(self, day: date, minute: float, student_id: str, catalog, window) -> dict | None:
        rng = self.rng
        created = _utc(day, 0, minute)
        hour = int(minute // 60)
        lines = self.build_items(hour, catalog)
        if not lines:
            return None
        items, total = self.order_items(lines)
        prep_estimate = max(i["prep_time_minutes"] for i in items)
        has_lunch = any((i["category"] or "").lower() in LUNCH_CATEGORIES for i in items)

        requested = None
        if has_lunch and hour < 11 and rng.random() < 0.5:
            requested = _utc(day, 0, rng.choice([12 * 60, 12 * 60 + 30, 13 * 60, 13 * 60 + 30]))
        elif rng.random() < 0.08:
            requested = created + timedelta(minutes=rng.choice([30, 45, 60]))
        close_utc = _utc(day, 0, window[1])
        if requested and requested > close_utc - timedelta(minutes=5):
            requested = None

        station_id = self.station["id"]
        outcome = _pick(rng, OUTCOMES)
        order = {
            "student_id": student_id,
            "restaurant_id": self.restaurant["id"],
            "items": items,
            "pipeline_status": None,
            "payment_status": "pending",
            "total_amount": total,
            "requested_ready_time": requested,
            "prep_start_time": None,
            "ready_at": None,
            "otp_code": None,
            "payment_reference": None,
            "request_number": None,
            "order_number": None,
            "station_queue": [station_id],
            "offered_kds_id": station_id,
            "declined_kds_ids": [],
            "assigned_kds_id": None,
            "payment_expires_at": None,
            "offered_at": created,
            "accepted_at": None,
            "paid_at": None,
            "preparation_started_at": None,
            "ready_marked_at": None,
            "delivered_at": None,
            "cancelled_at": None,
            "cancel_reason": None,
            "local_date": day.isoformat(),
            "created_at": created,
            "updated_at": created,
            "seed_tag": SEED_TAG,
            "_events": [("order_created", created, None, {"station_username": self.station["username"]})],
        }
        kds_actor = {"type": "kds", "id": station_id, "username": self.station["username"]}

        if outcome == "cancelled":
            at = created + timedelta(minutes=rng.uniform(1, 6))
            order.update(pipeline_status="cancelled", cancelled_at=at, cancel_reason="cancelled", updated_at=at)
            order["_events"].append(("order_cancelled", at, kds_actor, {}))
            return order

        accepted = created + timedelta(minutes=rng.uniform(0.3, 3.5))
        if outcome == "rejected":
            order.update(
                pipeline_status="rejected", declined_kds_ids=[station_id], cancelled_at=accepted,
                cancel_reason="rejected_by_all_stations", updated_at=accepted,
            )
            order["_events"].append(("order_rejected", accepted, kds_actor, {}))
            return order

        expires = accepted + timedelta(minutes=settings.payment.asap_minutes if requested is None else 15)
        order.update(
            pipeline_status="accepted", assigned_kds_id=station_id, accepted_at=accepted, payment_expires_at=expires,
            payment_reference=f"{settings.auth.backend_base_url}/mock-bac-checkout/seed?ref={rng.getrandbits(40):010x}",
        )
        order["_events"] += [("order_accepted", accepted, kds_actor, {}), ("payment_link_sent", accepted, None, {})]
        order["_events"].append(("customer_notified", accepted + timedelta(seconds=rng.uniform(2, 20)), None, {"notification": "link de pago"}))

        if outcome == "abandoned":
            expired = rng.random() < 0.6
            at = expires if expired else accepted + timedelta(minutes=rng.uniform(1, 8))
            order.update(
                pipeline_status="cancelled", payment_status="failed", cancelled_at=at, updated_at=at,
                cancel_reason="payment_link_expired" if expired else "payment_failed",
            )
            order["_events"].append(("payment_link_expired" if expired else "order_payment_failed", at, None, {}))
            return order

        paid = accepted + timedelta(minutes=min(rng.expovariate(1 / 2.5) + 0.4, (expires - accepted).total_seconds() / 60 - 0.2))
        peak_factor = 1.25 if hour in (12, 13) else 1.0
        prep_start = max(paid + timedelta(minutes=rng.uniform(0.5, 4) * peak_factor),
                         (requested - timedelta(minutes=prep_estimate)) if requested else paid)
        ready_estimate = prep_start + timedelta(minutes=prep_estimate)
        started = prep_start + timedelta(minutes=max(0.0, rng.gauss(1.0, 1.2)))
        actual = prep_estimate * rng.uniform(0.75, 1.35) * peak_factor + rng.gauss(0, 1)
        if has_lunch:
            actual = max(3.0, actual * 0.6)  # el almuerzo sale empacado: solo se arma y se entrega
        ready = started + timedelta(minutes=max(1.5, actual))
        delivered = ready + timedelta(minutes=rng.expovariate(1 / 5) + 0.5)
        order.update(
            payment_status="paid", paid_at=paid, prep_start_time=prep_start, ready_at=ready_estimate,
            otp_code=_otp(rng), preparation_started_at=started, ready_marked_at=ready,
            delivered_at=delivered, pipeline_status="delivered", updated_at=delivered,
        )
        order["_events"] += [
            ("order_paid", paid, None, {}),
            ("customer_notified", paid + timedelta(seconds=rng.uniform(2, 15)), None, {"notification": "pago confirmado"}),
            ("order_in_preparation", started, kds_actor, {}),
            ("order_ready", ready, kds_actor, {}),
            ("customer_notified", ready + timedelta(seconds=rng.uniform(2, 15)), None, {"notification": "orden lista"}),
        ]
        if rng.random() < 0.04:
            order["_events"].append(("otp_failed", delivered - timedelta(seconds=40), kds_actor, {}))
        order["_events"].append(("order_delivered", delivered, kds_actor, {}))
        return order


def _student_pool(rng: random.Random, size: int) -> tuple[list[str], list[float]]:
    ids = [f"SIM-{2020 + rng.randint(0, 5)}{rng.randint(10000, 99999)}" for _ in range(size)]
    weights = [1 / ((rank + 1) ** 0.85) for rank in range(size)]
    return ids, weights


async def _reserve(db, name: str, count: int) -> int:
    """Reserva `count` números consecutivos de una secuencia y devuelve el primero."""
    if count == 0:
        return 0
    doc = await db.counters.find_one_and_update(
        {"_id": name}, {"$inc": {"seq": count}, "$setOnInsert": {"start": SEQUENCE_START.get(name, 0)}},
        upsert=True, return_document=True,
    )
    return SEQUENCE_START.get(name, 0) + doc["seq"] - count + 1


async def clear(db) -> tuple[int, int]:
    orders = await db.orders.delete_many({"seed_tag": SEED_TAG})
    events = await db.order_events.delete_many({"seed_tag": SEED_TAG})
    return orders.deleted_count, events.deleted_count


async def pick_station(db, restaurant_id: str, username: str | None) -> dict:
    stations = [s async for s in db.kds_users.find({"restaurant_id": restaurant_id})]
    for s in stations:
        s["id"] = str(s["_id"])
    if username:
        match = next((s for s in stations if s["username"] == username), None)
        if match is None:
            raise SystemExit(f"No existe el usuario KDS '{username}' en {RESTAURANT_NAME}.")
        return match
    active = [s for s in stations if s.get("active", True)]
    if len(active) == 1:
        return active[0]
    full = [s for s in active if normalize_menu_filter(s.get("menu_filter"))["mode"] == "all"]
    if len(full) == 1:
        return full[0]
    if not active:
        raise SystemExit(f"{RESTAURANT_NAME} no tiene usuarios KDS activos. Crea uno desde el panel o con scripts/seed_kds_user.py.")
    names = ", ".join(s["username"] for s in active)
    raise SystemExit(f"{RESTAURANT_NAME} tiene varias estaciones ({names}). Indica cuál con --kds-username.")


async def main(args) -> None:
    if settings.environment.upper() == "PRODUCTION" and not args.allow_production:
        raise SystemExit("ENVIRONMENT=PRODUCTION: agrega --allow-production si de verdad quieres poblar esta base.")

    client = AsyncIOMotorClient(settings.mongo.uri, tz_aware=True)
    db = client[settings.mongo.db_name]

    if args.clear or not args.keep:
        deleted_orders, deleted_events = await clear(db)
        print(f"Borrados de la corrida anterior: {deleted_orders} pedidos, {deleted_events} eventos.")
        if args.clear:
            client.close()
            return

    restaurant = await db.restaurants.find_one({"name": RESTAURANT_NAME})
    if restaurant is None:
        client.close()
        raise SystemExit(f"No existe '{RESTAURANT_NAME}'. Corre primero: python -m scripts.seed_menu_test_data")
    restaurant["id"] = str(restaurant["_id"])
    menu = [m async for m in db.menu_items.find({"restaurant_id": restaurant["id"], "deleted": {"$ne": True}})]
    for item in menu:
        item["id"] = str(item["_id"])
        item["schedule_type"] = resolve_schedule_type(item)
    station = await pick_station(db, restaurant["id"], args.kds_username)
    if normalize_menu_filter(station.get("menu_filter"))["mode"] != "all":
        print(f"(!) La estación '{station['username']}' no tiene filtro 'all'; los pedidos generados igual se le asignan.")

    rng = random.Random(args.seed)
    generator = Generator(rng, restaurant, menu, station, args)
    end = local_today() if args.include_today else local_today() - timedelta(days=1)
    days = [end - timedelta(days=offset) for offset in range(args.days - 1, -1, -1)]
    now = datetime.now(timezone.utc)

    planned = []
    for index, day in enumerate(days):
        window = generator.open_hours(day)
        if window is None:
            continue
        planned.append((day, window, generator.orders_for_day(day, index, len(days))))
    students, weights = _student_pool(rng, max(50, int(sum(p[2] for p in planned) * 0.45)))

    orders: list[dict] = []
    for day, window, count in planned:
        catalog = generator.catalog_for(day)
        limit = now - timedelta(minutes=45) if day == local_today() else None
        for _ in range(count):
            minute = generator.random_minute(window, limit, day)
            if minute is None:
                continue
            student = rng.choices(students, weights=weights)[0]
            order = generator.build_order(day, minute, student, catalog, window)
            if order:
                orders.append(order)
    orders.sort(key=lambda o: o["created_at"])

    first_request = await _reserve(db, "request_number", len(orders))
    paid_orders = [o for o in orders if o["paid_at"]]
    first_order = await _reserve(db, "order_number", len(paid_orders))
    for i, order in enumerate(orders):
        order["request_number"] = first_request + i
    for i, order in enumerate(sorted(paid_orders, key=lambda o: o["paid_at"])):
        order["order_number"] = first_order + i

    events = []
    for order in orders:
        for event, at, actor, extra in order.pop("_events"):
            events.append((at, event, actor, extra, order))
    events.sort(key=lambda e: e[0])
    first_seq = await _reserve(db, "event_seq", len(events))

    for chunk in range(0, len(orders), 1000):
        result = await db.orders.insert_many(orders[chunk:chunk + 1000])
        for order, inserted_id in zip(orders[chunk:chunk + 1000], result.inserted_ids):
            order["id"] = str(inserted_id)

    event_docs = []
    for i, (at, event, actor, extra, order) in enumerate(events):
        is_order = bool(order["paid_at"]) and at >= order["paid_at"]
        view = order if is_order else {**order, "order_number": None}
        event_docs.append(
            {
                "seq": first_seq + i,
                "event": event,
                "restaurant_id": order["restaurant_id"],
                "kds_user_ids": [station["id"]],
                "order_id": order["id"],
                "request_number": order["request_number"],
                "order_number": view["order_number"],
                "pipeline_status": order["pipeline_status"],
                "payment_status": order["payment_status"],
                "actor": actor or {"type": "system", "id": None, "username": None},
                "message": describe_event(event, view, extra, actor),
                "data": {"total_amount": order["total_amount"], **{k: v for k, v in extra.items() if isinstance(v, str)}},
                "created_at": at,
                "local_date": at.astimezone(GUATEMALA_TZ).date().isoformat(),
                "seed_tag": SEED_TAG,
            }
        )
    if not args.no_events:
        for chunk in range(0, len(event_docs), 2000):
            await db.order_events.insert_many(event_docs[chunk:chunk + 2000])

    # --- Resumen con la misma lógica de /api/metrics/kpis --------------------------------------
    kpis = compute_kpis(orders, orders)
    per_day = Counter(o["local_date"] for o in orders)
    print(f"\n{RESTAURANT_NAME} (restaurant_id={restaurant['id']}), estación KDS: {station['username']}")
    print(f"Periodo: {days[0].isoformat()} a {days[-1].isoformat()} ({len(per_day)} días con servicio)")
    print(f"Pedidos: {len(orders)} (entre {min(per_day.values())} y {max(per_day.values())} por día)")
    print(f"Eventos: {0 if args.no_events else len(event_docs)}")
    print(f"Ventas: Q{kpis['ventas']['total']:,.2f}  | ticket promedio Q{kpis['resumen']['ticket_promedio']:.2f}")
    top = kpis["plato_mas_pedido"]["ranking"][:5]
    print("Más vendidos: " + ", ".join(f"{t['nombre']} ({t['cantidad']})" for t in top))
    print(f"Preparación: promedio {kpis['tiempo_preparacion_min']['promedio']} min, p90 {kpis['tiempo_preparacion_min']['p90']} min")
    peak = kpis["hora_pico"]["hora_pico_global"]
    print(f"Hora pico: {peak['etiqueta']} ({peak['promedio_por_dia']} pedidos/día)")
    print(f"Abandono: {kpis['abandono']['tasa']:.1%}  | rechazados: {kpis['abandono']['rechazados_por_cocina']}")
    r = kpis["recompra"]
    print(f"Recompra: {r['tasa_recompra']:.1%} ({r['usuarios_2_o_mas']} de {r['total_usuarios']} usuarios)")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="Días hacia atrás a poblar (default 30)")
    parser.add_argument("--include-today", action="store_true", help="Incluye hoy (solo hasta hace 45 minutos)")
    parser.add_argument("--min-per-day", type=int, default=50)
    parser.add_argument("--max-per-day", type=int, default=80)
    parser.add_argument("--saturday-min", type=int, default=20)
    parser.add_argument("--saturday-max", type=int, default=35)
    parser.add_argument("--kds-username", default=None, help="Estación que atiende los pedidos (default: la única activa)")
    parser.add_argument("--seed", type=int, default=2026, help="Semilla aleatoria (mismos datos en cada corrida)")
    parser.add_argument("--keep", action="store_true", help="No borra lo generado en corridas anteriores")
    parser.add_argument("--clear", action="store_true", help="Solo borra lo generado y termina")
    parser.add_argument("--no-events", action="store_true", help="No genera order_events (solo pedidos)")
    parser.add_argument("--allow-production", action="store_true")
    asyncio.run(main(parser.parse_args()))
