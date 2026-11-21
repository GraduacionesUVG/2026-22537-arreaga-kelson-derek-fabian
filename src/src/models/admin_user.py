"""
Forma del documento de Mongo para `admin_users` — login del panel de
administración, correo + contraseña -> JWT (ver src/core/security.py).

Roles:

- `restaurant_admin`: administra un solo restaurante (`restaurant_id` obligatorio).
- `superadmin`: equipo Dev/TI, ve y administra todos los restaurantes.

El rol histórico `admin` se interpreta con `normalize_admin_role`: con
`restaurant_id` equivale a `restaurant_admin`, sin él a `superadmin`.

`must_change_password` nace en `true`: la contraseña inicial se define con el
enlace de restablecimiento enviado por correo, o se cambia en el primer login.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class AdminRole(str, Enum):
    RESTAURANT_ADMIN = "restaurant_admin"
    SUPERADMIN = "superadmin"


LEGACY_ADMIN_ROLE = "admin"
ADMIN_ROLES = (AdminRole.RESTAURANT_ADMIN.value, AdminRole.SUPERADMIN.value)

COLLECTION_NAME = "admin_users"


def normalize_admin_role(role: str | None, restaurant_id: str | None) -> str:
    if role in ADMIN_ROLES:
        return role
    return AdminRole.RESTAURANT_ADMIN.value if restaurant_id else AdminRole.SUPERADMIN.value


@dataclass
class AdminUserDocument:
    """
    Ejemplo de documento:
    {
        "_id": ObjectId("..."),
        "email": "admin@goldenharvest.com",
        "username": "Admin Golden Harvest",
        "password_hash": "$2b$12$...",
        "restaurant_name": "Golden Harvest",
        "restaurant_id": "665f1c...",
        "role": "restaurant_admin",
        "must_change_password": true,
        "created_at": ISODate("2026-07-25T10:00:00Z"),
        "last_login_at": null,
        "password_changed_at": null
    }
    """

    email: str
    username: str
    password_hash: str | None
    role: AdminRole = AdminRole.RESTAURANT_ADMIN
    restaurant_name: str | None = None
    restaurant_id: str | None = None
    must_change_password: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: datetime | None = None
    password_changed_at: datetime | None = None
