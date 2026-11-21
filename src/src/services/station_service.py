"""
Asignación de solicitudes a estaciones KDS.

1. Candidatas: estaciones activas del restaurante que cubren TODOS los
   productos del pedido (`menu_filter`). Si ninguna los cubre todos, el
   pedido se rechaza pidiendo ordenar por separado.
2. Orden de oferta: conectadas primero, luego `priority` (1 = más alta),
   luego menor carga (minutos de cola pagada), luego menos pedidos del día.
   Con igual prioridad, la carga se reparte.
3. La solicitud se ofrece a una sola estación a la vez (ver OrderService).

Un restaurante sin usuarios KDS no usa estaciones: el pedido lo ve todo el restaurante.
"""

from datetime import datetime, timedelta

from src.core.realtime import kds_manager
from src.core.timeutils import as_utc, local_date_str, utc_now
from src.models.kds_user import station_covers_item
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.order_repo import OrderRepository


class StationError(ValueError):
    """El pedido no se puede asignar a ninguna estación."""


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " y " + names[-1]


def busy_minutes_from(intervals: list[tuple[datetime, datetime]], now: datetime) -> float:
    """Minutos desde `now` hasta el fin del bloque continuo de trabajo que está en curso."""
    busy_until = now
    for start, end in sorted(intervals):
        if start <= busy_until:
            busy_until = max(busy_until, end)
    return (busy_until - now).total_seconds() / 60


class StationService:
    def __init__(self, kds_repo: KdsUserRepository | None, order_repo: OrderRepository):
        self.kds_repo = kds_repo
        self.order_repo = order_repo

    async def busy_intervals(self, restaurant_id: str, kds_user_id: str | None = None) -> list[tuple[datetime, datetime]]:
        """Bloques `(inicio, fin)` ya comprometidos por la cola pagada (de la estación, o de todo el restaurante)."""
        queue = await self.order_repo.find_kitchen_queue(restaurant_id, kds_user_id)
        intervals = []
        for order in queue:
            ready_at = as_utc(order.get("ready_at"))
            if ready_at is None:
                continue
            start = as_utc(order.get("prep_start_time"))
            if start is None:
                prep = max((i.get("prep_time_minutes", 0.0) for i in order.get("items", [])), default=0.0)
                start = ready_at - timedelta(minutes=prep)
            intervals.append((start, ready_at))
        return sorted(intervals)

    async def backlog_minutes(self, restaurant_id: str, now: datetime, kds_user_id: str | None = None) -> float:
        """Minutos hasta que la cocina queda libre, contando solo el trabajo que empieza ya o encadenado sin pausa."""
        return busy_minutes_from(await self.busy_intervals(restaurant_id, kds_user_id), now)

    async def restaurant_stations(self, restaurant_id: str) -> list[dict]:
        if self.kds_repo is None:
            return []
        return await self.kds_repo.find_for_restaurant(restaurant_id)

    async def rank_candidates(self, restaurant_id: str, menu_items: list[dict]) -> list[dict]:
        """Estaciones que pueden recibir el pedido, en orden de oferta.

        Devuelve `[]` si el restaurante no tiene estaciones configuradas.
        Lanza StationError si tiene estaciones pero ninguna cubre todo el pedido.
        """
        stations = await self.restaurant_stations(restaurant_id)
        if not stations:
            return []
        active = [s for s in stations if s.get("active", True)]
        if not active:
            raise StationError("El restaurante no está recibiendo pedidos en este momento.")

        covering = [s for s in active if all(station_covers_item(s.get("menu_filter"), item) for item in menu_items)]
        if not covering:
            raise StationError(self._split_message(active, menu_items))

        now = utc_now()
        today = local_date_str(now)
        ranked = []
        for station in covering:
            ranked.append(
                (
                    0 if kds_manager.is_kds_connected(station["id"]) else 1,
                    int(station.get("priority") or 1),
                    await self.backlog_minutes(restaurant_id, now, station["id"]),
                    await self.order_repo.count_active_for_station(station["id"]),
                    await self.order_repo.count_for_station_on(station["id"], today),
                    station.get("username", ""),
                    station,
                )
            )
        ranked.sort(key=lambda row: row[:6])
        return [row[-1] for row in ranked]

    @staticmethod
    def _split_message(stations: list[dict], menu_items: list[dict]) -> str:
        uncovered = [i["name"] for i in menu_items if not any(station_covers_item(s.get("menu_filter"), i) for s in stations)]
        if uncovered:
            return f"{_join(uncovered)} no está disponible por el momento."
        groups: dict[str, list[str]] = {}
        for item in menu_items:
            station = next(s for s in stations if station_covers_item(s.get("menu_filter"), item))
            groups.setdefault(station["id"], []).append(item["name"])
        parts = [_join(names) for names in groups.values()]
        return "Estos productos se deben ordenar por separado: " + "; ".join(parts) + "."
