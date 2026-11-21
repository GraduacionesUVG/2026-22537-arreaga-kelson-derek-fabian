"""
Crea y lista usuarios de administración y estaciones KDS usando la API
(local o producción con --base-url). Funciona en cualquier terminal.

Comandos:

    crear-kds       Crea una estación KDS (requiere login de admin del restaurante)
    listar-kds      Lista las estaciones KDS y su estado de conexión
    crear-admin     Crea un admin para un restaurante existente (requiere login de superadmin)
    listar-admins   Lista los admins de restaurantes (requiere login de superadmin)
    restaurantes    Lista los restaurantes con su id (requiere login de superadmin)

Ejemplos:

    # KDS que recibe todo el menú, con el admin de Golden Harvest
    uv run python scripts/admin_users_tool.py crear-kds --kds-username gh-kds --pin 1234

    # KDS solo para ciertas categorías
    uv run python scripts/admin_users_tool.py crear-kds --kds-username gh-almuerzos --pin 1234 --categorias "almuerzos" "almuerzos del día"

    # Admin nuevo para Golden Harvest (con superadmin). Sin --admin-password se envía un correo para crear la contraseña.
    uv run python scripts/admin_users_tool.py crear-admin --email ti@uvg.edu.gt --password CLAVE_TI --restaurante "Golden Harvest" --nuevo-nombre "Gerente GH" --nuevo-email gerente@goldenharvest.com --admin-password ClaveInicial1

    # En producción
    uv run python scripts/admin_users_tool.py listar-kds --base-url https://gp-agent-backend.onrender.com --password CLAVE

El primer superadmin de una base nueva no se puede crear por la API: se crea con
`python -m scripts.seed_admin_user` (rol 2) apuntando MONGO_URI a esa base.

No importa `src`, se corre directo (sin -m).
"""

import argparse
import sys

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_EMAIL = "admin@goldenharvest.com"
DEFAULT_PASSWORD = "Password123"


def _fail(response: httpx.Response) -> None:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text
    if isinstance(detail, list):  # errores de validación de FastAPI
        detail = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg')}" for e in detail)
    sys.exit(f"Error {response.status_code}: {detail}")


def _login(client: httpx.Client, email: str, password: str) -> tuple[dict, dict]:
    response = client.post("/auth/admin/login", json={"email": email, "password": password})
    if response.status_code != 200:
        print(f"No se pudo iniciar sesión con {email}.")
        _fail(response)
    data = response.json()
    print(f"Sesión: {data.get('username') or email} ({data.get('role')}"
          f"{', ' + data['restaurant_name'] if data.get('restaurant_name') else ''})")
    return {"Authorization": f"Bearer {data['access_token']}"}, data


def _restaurant_id(client: httpx.Client, headers: dict, restaurant_id: str | None, name: str | None) -> str:
    if restaurant_id:
        return restaurant_id
    response = client.get("/api/ti/restaurants", headers=headers)
    if response.status_code != 200:
        _fail(response)
    matches = [r for r in response.json() if r["name"].strip().lower() == (name or "").strip().lower()]
    if not matches:
        sys.exit(f"No existe el restaurante '{name}'. Usa el comando 'restaurantes' para ver los nombres.")
    return matches[0]["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["crear-kds", "listar-kds", "crear-admin", "listar-admins", "restaurantes"])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="Correo con el que se inicia sesión")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Contraseña con la que se inicia sesión")
    parser.add_argument("--restaurant-id", default=None, help="Id del restaurante (superadmin)")
    parser.add_argument("--restaurante", default=None, help="Nombre del restaurante (superadmin), en lugar del id")
    # crear-kds
    parser.add_argument("--kds-username", help="Usuario de la estación KDS")
    parser.add_argument("--pin", help="PIN de 4 a 8 dígitos")
    parser.add_argument("--prioridad", type=int, default=1, help="1 = más alta")
    parser.add_argument("--categorias", nargs="*", default=None, help="Categorías que recibe (sin esto recibe todo)")
    # crear-admin
    parser.add_argument("--nuevo-nombre", help="Nombre del nuevo admin")
    parser.add_argument("--nuevo-email", help="Correo del nuevo admin")
    parser.add_argument("--admin-password", default=None, help="Contraseña inicial (sin esto se envía un correo)")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=90.0) as client:
        headers, session = _login(client, args.email, args.password)
        superadmin = session.get("role") == "superadmin"

        if args.command in ("crear-kds", "listar-kds"):
            params = {}
            if superadmin:
                if args.command == "crear-kds" or args.restaurant_id or args.restaurante:
                    params["restaurant_id"] = _restaurant_id(client, headers, args.restaurant_id, args.restaurante)
                else:
                    params["restaurant_id"] = "all"

            if args.command == "crear-kds":
                if not args.kds_username or not args.pin:
                    parser.error("crear-kds necesita --kds-username y --pin.")
                menu_filter = (
                    {"mode": "custom", "item_ids": [], "categories": args.categorias}
                    if args.categorias
                    else {"mode": "all"}
                )
                body = {"username": args.kds_username, "pin": args.pin, "priority": args.prioridad, "menu_filter": menu_filter, "active": True}
                response = client.post("/api/admin/kds-users", json=body, headers=headers, params=params)
                if response.status_code != 201:
                    _fail(response)
                station = response.json()
                recibe = "todo el menú" if menu_filter["mode"] == "all" else ", ".join(args.categorias)
                print(f"KDS creado: {station['username']} ({station['restaurant_name']}), PIN {args.pin}, recibe {recibe}.")
                print(f"Login en el KDS: POST /auth/kds/login con usuario '{station['username']}' y el PIN.")
                return

            response = client.get("/api/admin/kds-users", headers=headers, params=params)
            if response.status_code != 200:
                _fail(response)
            for s in response.json():
                f = s["menu_filter"]
                recibe = "todo" if f["mode"] == "all" else ", ".join(f["categories"] + [f"{len(f['item_ids'])} productos"] * bool(f["item_ids"]))
                estado = "conectado" if s["connected"] else "desconectado"
                activo = "" if s["active"] else " [pausado]"
                print(f"- {s['username']:<18} {s['restaurant_name'] or '':<16} prioridad {s['priority']}  {estado}{activo}  recibe: {recibe}")
            return

        if not superadmin:
            sys.exit("Este comando necesita un usuario superadmin (Dev/TI). Pasa --email/--password de un superadmin.")

        if args.command == "restaurantes":
            response = client.get("/api/ti/restaurants", headers=headers)
            if response.status_code != 200:
                _fail(response)
            for r in response.json():
                print(f"- {r['name']:<20} id={r['id']}  admins={r['admins']}  kds={r['kds_users']}")
            return

        if args.command == "listar-admins":
            response = client.get("/api/ti/restaurant-users", headers=headers)
            if response.status_code != 200:
                _fail(response)
            for a in response.json():
                estado = "sin contraseña" if a["password_pending"] else ("debe cambiarla" if a["must_change_password"] else "activa")
                print(f"- {a['email']:<32} {a['restaurant_name'] or '':<16} contraseña: {estado}")
            return

        # crear-admin
        if not args.nuevo_nombre or not args.nuevo_email:
            parser.error("crear-admin necesita --nuevo-nombre y --nuevo-email (y --restaurante o --restaurant-id).")
        body = {
            "restaurant_id": _restaurant_id(client, headers, args.restaurant_id, args.restaurante),
            "username": args.nuevo_nombre,
            "email": args.nuevo_email,
        }
        if args.admin_password:
            body["password"] = args.admin_password
        response = client.post("/api/ti/restaurant-users", json=body, headers=headers)
        if response.status_code != 201:
            _fail(response)
        result = response.json()
        admin = result["admin"]
        print(f"Admin creado: {admin['email']} ({admin['restaurant_name']}).")
        if args.admin_password:
            print("Contraseña inicial asignada; deberá cambiarla en su primer login.")
        elif result["email_sent"]:
            print("Se envió el correo para crear la contraseña (válido 60 minutos).")
        else:
            print("(!) No se pudo enviar el correo. Reenvíalo desde el panel Dev/TI o crea el admin con --admin-password.")


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        sys.exit("No se pudo conectar a la API. Revisa --base-url y que el servidor esté arriba.")
