"""Reglas de negocio de restaurantes: listado, horario y ubicación."""

from src.core.timeutils import utc_now
from src.models.restaurant import is_open_at, normalize_opening_hours, service_fee_for
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.schemas.restaurant import RestaurantCreate
from src.services.payment_accounts import is_configured as payment_gateway_configured
from src.services.restaurant_availability import public_availability, restaurant_availability


class RestaurantService:
    def __init__(self, repo: RestaurantRepository, kds_repo: KdsUserRepository | None = None):
        self.repo = repo
        self.kds_repo = kds_repo

    async def _stations(self, restaurant_id: str) -> list[dict]:
        if self.kds_repo is None:
            return []
        return await self.kds_repo.find_for_restaurant(restaurant_id)

    async def with_availability(self, restaurant: dict) -> dict:
        """Restaurante + `status` ("open"/"closed") y demás campos de disponibilidad de ahora."""
        availability = restaurant_availability(restaurant, await self._stations(restaurant["id"]), utc_now())
        return {**restaurant, **public_availability(availability)}

    async def list_public(self, active_only: bool = False) -> list[dict]:
        return [await self.with_availability(r) for r in await self.list_restaurants(active_only)]

    async def get_public(self, restaurant_id: str) -> dict | None:
        restaurant = await self.get_restaurant(restaurant_id)
        return await self.with_availability(restaurant) if restaurant else None

    async def availability_by_id(self, restaurant_ids: set[str]) -> dict[str, dict]:
        """`{restaurant_id: disponibilidad}` para anotar productos del menú."""
        result = {}
        for restaurant_id in restaurant_ids:
            restaurant = await self.get_restaurant(restaurant_id)
            if restaurant is not None:
                result[restaurant_id] = public_availability(
                    restaurant_availability(restaurant, await self._stations(restaurant_id), utc_now())
                )
        return result

    async def list_restaurants(self, active_only: bool = False) -> list[dict]:
        filter_ = {"active": True} if active_only else None
        return await self.repo.find_all(filter_, sort=[("name", 1)])

    async def get_restaurant(self, restaurant_id: str) -> dict | None:
        return await self.repo.find_by_id(restaurant_id)

    async def create_restaurant(self, data: RestaurantCreate) -> dict:
        payload = data.model_dump()
        payload["opening_hours"] = normalize_opening_hours(payload.get("opening_hours"))
        return await self.repo.insert_one(payload)

    @staticmethod
    def admin_view(restaurant: dict) -> dict:
        return {
            "id": restaurant["id"],
            "name": restaurant["name"],
            "location": restaurant.get("location"),
            "active": restaurant.get("active", True),
            "service_fee": service_fee_for(restaurant),
            "opening_hours": normalize_opening_hours(restaurant.get("opening_hours")),
            "is_open_now": is_open_at(restaurant, utc_now()),
            "payment_gateway_configured": payment_gateway_configured(restaurant),
        }

    async def update_location(self, restaurant_id: str, location: str) -> dict | None:
        return await self.repo.update_one(restaurant_id, {"location": location.strip()})

    async def update_service_fee(self, restaurant_id: str, service_fee: float) -> dict | None:
        """Solo afecta pedidos nuevos: cada pedido guarda la tarifa con la que se creó."""
        return await self.repo.update_one(restaurant_id, {"service_fee": round(max(0.0, service_fee), 2)})

    async def update_opening_hours(self, restaurant_id: str, opening_hours: dict) -> dict | None:
        return await self.repo.update_one(restaurant_id, {"opening_hours": normalize_opening_hours(opening_hours)})
