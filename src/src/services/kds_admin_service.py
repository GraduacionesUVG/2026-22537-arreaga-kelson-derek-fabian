"""Administración de estaciones KDS: alta, usuario, PIN, prioridad, filtro de menú y estado."""

from src.core.realtime import kds_manager
from src.core.security import hash_pin
from src.core.timeutils import utc_now
from src.models.kds_user import normalize_menu_filter
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuRepository
from src.repositories.restaurant_repo import RestaurantRepository


class KdsAdminError(ValueError):
    pass


def kds_view(station: dict) -> dict:
    status = kds_manager.kds_status(station["id"])
    return {
        "kds_user_id": station["id"],
        "username": station["username"],
        "restaurant_id": station.get("restaurant_id"),
        "restaurant_name": station.get("restaurant_name"),
        "priority": int(station.get("priority") or 1),
        "active": station.get("active", True),
        "menu_filter": normalize_menu_filter(station.get("menu_filter")),
        "last_login_at": station.get("last_login_at"),
        **status,
    }


def status_view(station: dict) -> dict:
    view = kds_view(station)
    return {k: view[k] for k in ("kds_user_id", "username", "restaurant_id", "restaurant_name", "connected", "connected_since", "last_seen")}


class KdsAdminService:
    def __init__(self, kds_repo: KdsUserRepository, restaurant_repo: RestaurantRepository, menu_repo: MenuRepository):
        self.kds_repo = kds_repo
        self.restaurant_repo = restaurant_repo
        self.menu_repo = menu_repo

    async def list_stations(self, restaurant_id: str | None) -> list[dict]:
        return [kds_view(s) for s in await self.kds_repo.find_for_restaurant(restaurant_id)]

    async def statuses(self, restaurant_id: str | None) -> list[dict]:
        return [status_view(s) for s in await self.kds_repo.find_for_restaurant(restaurant_id)]

    async def get(self, restaurant_id: str | None, kds_user_id: str) -> dict | None:
        try:
            station = await self.kds_repo.find_by_id(kds_user_id)
        except ValueError:
            return None
        if station is None or (restaurant_id and station.get("restaurant_id") != restaurant_id):
            return None
        return station

    async def _check_username(self, username: str, exclude_id: str | None = None) -> str:
        username = username.strip()
        other = await self.kds_repo.find_by_username(username)
        if other and other["id"] != exclude_id:
            raise KdsAdminError(f"El usuario '{username}' ya existe.")
        return username

    async def _validate_filter(self, restaurant_id: str, menu_filter: dict) -> dict:
        menu_filter = normalize_menu_filter(menu_filter)
        if menu_filter["mode"] == "custom":
            if not menu_filter["item_ids"] and not menu_filter["categories"]:
                raise KdsAdminError("Selecciona al menos un producto o una categoría, o usa 'all'.")
            valid_ids = {i["id"] for i in await self.menu_repo.find_for_restaurant(restaurant_id)}
            unknown = [i for i in menu_filter["item_ids"] if i not in valid_ids]
            if unknown:
                raise KdsAdminError(f"Productos que no pertenecen al restaurante: {', '.join(unknown)}")
            menu_filter["categories"] = [c.strip() for c in menu_filter["categories"] if c.strip()]
        return menu_filter

    async def create(self, restaurant_id: str, data: dict) -> dict:
        restaurant = await self.restaurant_repo.find_by_id(restaurant_id)
        if restaurant is None:
            raise KdsAdminError("Restaurante no encontrado.")
        username = await self._check_username(data["username"])
        station = await self.kds_repo.insert_one(
            {
                "username": username,
                "pin_hash": hash_pin(data["pin"]),
                "restaurant_name": restaurant["name"],
                "restaurant_id": restaurant_id,
                "menu_filter": await self._validate_filter(restaurant_id, data.get("menu_filter") or {}),
                "priority": int(data.get("priority") or 1),
                "active": bool(data.get("active", True)),
                "token_version": 0,
                "created_at": utc_now(),
                "last_login_at": None,
            }
        )
        return kds_view(station)

    async def update_username(self, station: dict, username: str) -> dict:
        username = await self._check_username(username, station["id"])
        return kds_view(await self.kds_repo.increment_token_version(station["id"], {"username": username}))

    async def update_pin(self, station: dict, pin: str) -> dict:
        return kds_view(await self.kds_repo.increment_token_version(station["id"], {"pin_hash": hash_pin(pin)}))

    async def update_priority(self, station: dict, priority: int) -> dict:
        return kds_view(await self.kds_repo.update_one(station["id"], {"priority": int(priority)}))

    async def update_active(self, station: dict, active: bool, actor: dict | None = None) -> dict:
        """Pausa (`active=False`) o reanuda la estación. Una estación pausada no recibe
        solicitudes nuevas; si todas las del restaurante lo están, el restaurante sale
        "closed" (kitchen_out_of_service) para el chatbot. Se avisa en vivo a todas las
        pantallas con esa sesión abierta. Si la estación tiene un turno abierto, la pausa
        queda registrada en el turno (`kds_shifts.pauses`)."""
        from src.integrations.events import event_publisher
        from src.services.shift_service import ShiftService

        was_active = bool(station.get("active", True))
        updated = await self.kds_repo.update_one(station["id"], {"active": bool(active)})
        await ShiftService(self.kds_repo.collection.database).record_pause_change(
            station["id"], was_active, bool(active), actor
        )
        await event_publisher.publish_station_status(updated)
        return kds_view(updated)

    async def update_menu_filter(self, station: dict, menu_filter: dict) -> dict:
        menu_filter = await self._validate_filter(station["restaurant_id"], menu_filter)
        return kds_view(await self.kds_repo.update_one(station["id"], {"menu_filter": menu_filter}))
