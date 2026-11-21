"""
Forma del documento de Mongo para `kds_users` — pantalla de cocina (KDS) de
un restaurante. Cada usuario KDS es una **estación**: recibe solo los pedidos
cuyos productos cubre su `menu_filter`.

- `menu_filter.mode = "all"`: recibe todo el menú del restaurante.
- `menu_filter.mode = "custom"`: recibe los productos cuyo id está en
  `item_ids` o cuya categoría está en `categories`.
- `priority`: 1 es la más alta. Define a qué estación se ofrece primero una
  solicitud cuando varias pueden prepararla (ver src/services/station_service.py).
- `active = false`: la estación no recibe solicitudes nuevas.
- `token_version`: se incrementa al cambiar usuario o PIN; invalida los
  tokens emitidos antes.

`username` es único (se usa para el login con PIN).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

COLLECTION_NAME = "kds_users"

MENU_FILTER_ALL = {"mode": "all", "item_ids": [], "categories": []}


def normalize_menu_filter(menu_filter: dict | str | None) -> dict:
    if menu_filter in (None, "all") or (isinstance(menu_filter, dict) and menu_filter.get("mode") == "all"):
        return dict(MENU_FILTER_ALL)
    if isinstance(menu_filter, dict):
        return {
            "mode": "custom",
            "item_ids": list(menu_filter.get("item_ids") or []),
            "categories": list(menu_filter.get("categories") or []),
        }
    return dict(MENU_FILTER_ALL)


def station_covers_item(menu_filter: dict | None, menu_item: dict) -> bool:
    menu_filter = normalize_menu_filter(menu_filter)
    if menu_filter["mode"] == "all":
        return True
    if menu_item.get("id") in menu_filter["item_ids"]:
        return True
    categories = {c.strip().lower() for c in menu_filter["categories"]}
    return (menu_item.get("category") or "").strip().lower() in categories


@dataclass
class KdsUserDocument:
    """
    Ejemplo de documento:
    {
        "_id": ObjectId("..."),
        "username": "gh-spazio",
        "pin_hash": "$2b$12$...",
        "restaurant_name": "Golden Harvest",
        "restaurant_id": "665f1c...",
        "menu_filter": {"mode": "custom", "item_ids": [], "categories": ["almuerzos del día"]},
        "priority": 1,
        "active": true,
        "token_version": 0,
        "created_at": ISODate("2026-09-08T10:00:00Z"),
        "last_login_at": null
    }
    """

    username: str
    pin_hash: str
    restaurant_name: str
    restaurant_id: str | None = None
    menu_filter: dict = field(default_factory=lambda: dict(MENU_FILTER_ALL))
    priority: int = 1
    active: bool = True
    token_version: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: datetime | None = None
