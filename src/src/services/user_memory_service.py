"""
Memoria del agente de WhatsApp e historial de pedidos por estudiante.

Lo consume solo `agent/` (ver agent/memory.py): el historial se calcula aquí a
partir de `orders` -- son hechos, no algo que el LLM recuerde -- y la memoria
(preferences/summaries/session) se guarda tal cual en `user_memory`.
"""

from collections import Counter

from src.core.timeutils import to_local
from src.models.user_memory import normalize_user_memory
from src.repositories.order_repo import OrderRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.repositories.user_memory_repo import UserMemoryRepository
from src.schemas.user_memory import UserMemoryUpdate

TOP_N = 5
RECENT_ORDERS = 5


class UserMemoryService:
    def __init__(
        self,
        memory_repo: UserMemoryRepository,
        order_repo: OrderRepository,
        restaurant_repo: RestaurantRepository,
    ):
        self.memory_repo = memory_repo
        self.order_repo = order_repo
        self.restaurant_repo = restaurant_repo

    # --- Memoria ---------------------------------------------------------------

    async def get_memory(self, student_id: str) -> dict:
        """Siempre devuelve la forma completa (vacía si el estudiante aún no tiene memoria)."""
        doc = await self.memory_repo.find_by_student_id(student_id)
        return normalize_user_memory(doc, student_id)

    async def update_memory(self, student_id: str, data: UserMemoryUpdate) -> dict:
        sections = data.model_dump(exclude_none=True)
        if not sections:
            return await self.get_memory(student_id)
        doc = await self.memory_repo.upsert_sections(student_id, sections)
        return normalize_user_memory(doc, student_id)

    async def delete_memory(self, student_id: str) -> bool:
        return await self.memory_repo.delete_by_student_id(student_id)

    # --- Historial de pedidos --------------------------------------------------

    async def get_order_history(self, student_id: str) -> dict:
        orders = await self.order_repo.find_user_history(student_id)
        names = await self._restaurant_names({o["restaurant_id"] for o in orders})

        products: Counter[str] = Counter()
        for order in orders:
            for item in order.get("items", []):
                products[item["product_name"]] += int(item.get("quantity", 1))

        restaurants = Counter(names[o["restaurant_id"]] for o in orders)
        # Hora a la que suele querer su pedido: la que pidió, o la de creación
        # si pidió "lo antes posible".
        hours = Counter(
            to_local(o.get("requested_ready_time") or o["created_at"]).hour for o in orders
        )
        totals = [float(o.get("total_amount", 0)) for o in orders]

        return {
            "total_orders": len(orders),
            "most_ordered": [{"product": p, "count": c} for p, c in products.most_common(TOP_N)],
            "frequent_restaurants": [
                {"restaurant": r, "count": c} for r, c in restaurants.most_common(TOP_N)
            ],
            "recent_orders": [
                {
                    "date": o["created_at"],
                    "restaurant": names[o["restaurant_id"]],
                    "items": [f"{i.get('quantity', 1)}x {i['product_name']}" for i in o.get("items", [])],
                    "total": float(o.get("total_amount", 0)),
                }
                for o in orders[:RECENT_ORDERS]
            ],
            "tried_products": sorted(products),
            "usual_hour": f"{hours.most_common(1)[0][0]:02d}:00" if hours else None,
            "average_spend": round(sum(totals) / len(totals), 2) if totals else None,
        }

    async def _restaurant_names(self, restaurant_ids: set[str]) -> dict[str, str]:
        names = {}
        for restaurant_id in restaurant_ids:
            try:
                restaurant = await self.restaurant_repo.find_by_id(restaurant_id)
            except ValueError:  # id que no es ObjectId (datos de prueba viejos)
                restaurant = None
            names[restaurant_id] = restaurant["name"] if restaurant else "Restaurante"
        return names
