"""
Herramienta de línea de comandos para el menú de un restaurante usando la API
(sirve igual en local o en producción con --base-url). Funciona en Windows,
macOS y Linux; no necesita PowerShell ni curl.

Comandos:

    export              Descarga el menú general actual (.xlsx)
    import ARCHIVO      Sube un menú general (.xlsx). Con --dry-run solo valida
    calendar-template   Descarga la plantilla del calendario
    calendar-export     Descarga el calendario desde hoy
    calendar-import ARCHIVO   Sube un calendario (--dry-run, --accept-warnings)
    completar           Completa los platillos existentes con datos de ejemplo
                        (modificadores, listo desde, hora límite, porciones) sin
                        tocar lo que ya tenga datos; valida y luego guarda

Ejemplos:

    uv run python scripts/admin_menu_tool.py export --out menu_gh.xlsx
    uv run python scripts/admin_menu_tool.py import menu_gh.xlsx --dry-run
    uv run python scripts/admin_menu_tool.py import menu_gh.xlsx
    uv run python scripts/admin_menu_tool.py completar --dry-run
    uv run python scripts/admin_menu_tool.py completar
    uv run python scripts/admin_menu_tool.py calendar-import calendario.xlsx --accept-warnings
    uv run python scripts/admin_menu_tool.py completar --base-url https://gp-agent-backend.onrender.com --password TU_CLAVE

Credenciales por defecto: admin de Golden Harvest (admin@goldenharvest.com).
No importa `src`, se corre directo (sin -m).
"""

import argparse
import io
import json
import sys
from datetime import date

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_EMAIL = "admin@goldenharvest.com"
DEFAULT_PASSWORD = "Password123"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Datos de ejemplo por producto (nombre en minúsculas) y, si no hay coincidencia, por categoría.
EXAMPLE_BY_NAME = {
    "hamburguesa": {"modifiers": [("Quitar", "Sin tomate", 0), ("Quitar", "Sin cebolla", 0),
                                  ("Extras", "Doble queso", 5), ("Extras", "Tocino", 6)]},
    "baguette chapin": {"modifiers": [("Quitar", "Sin cebolla", 0), ("Extras", "Extra queso", 4),
                                      ("Extras", "Aguacate", 5)]},
    "sushi california roll": {"modifiers": [("Quitar", "Sin queso crema", 0), ("Extras", "Salsa de anguila", 3),
                                            ("Extras", "Tempura", 5)], "porciones_dia": 40},
    "sushi dragon roll": {"modifiers": [("Quitar", "Sin picante", 0), ("Extras", "Salsa de anguila", 3)],
                          "porciones_dia": 30},
    "almuerzo del día": {"modifiers": [("Quitar", "Sin ensalada", 0), ("Cambios", "Arroz en lugar de papas", 0),
                                       ("Extras", "Porción extra de proteína", 12)],
                         "listo_desde": "11:30", "hora_limite_pedido": "14:30", "porciones_dia": 60},
    "cena": {"listo_desde": "17:00", "hora_limite_pedido": "19:30", "porciones_dia": 25},
}
EXAMPLE_BY_CATEGORY = {
    "almuerzos": {"listo_desde": "11:30", "hora_limite_pedido": "14:30", "porciones_dia": 60},
    "almuerzos del día": {"listo_desde": "11:30", "hora_limite_pedido": "14:30", "porciones_dia": 60},
    "cenas": {"listo_desde": "17:00", "hora_limite_pedido": "19:30"},
    "comida": {"modifiers": [("Quitar", "Sin cebolla", 0), ("Extras", "Extra queso", 4)]},
    "sushi": {"modifiers": [("Extras", "Salsa extra", 2)]},
}


def _fail(response: httpx.Response) -> None:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text
    if isinstance(detail, dict):
        print(detail.get("message", ""))
        for item in detail.get("errors", []):
            print(f"  - hoja '{item.get('sheet')}', fila {item.get('row')}: {item.get('error')}")
        for item in detail.get("warnings", []):
            print(f"  - {item.get('message')}")
    else:
        print(f"Error {response.status_code}: {detail}")
    sys.exit(1)


def login(client: httpx.Client, email: str, password: str) -> dict:
    response = client.post("/auth/admin/login", json={"email": email, "password": password})
    if response.status_code != 200:
        print(f"No se pudo iniciar sesión con {email}.")
        _fail(response)
    data = response.json()
    print(f"Sesión iniciada: {data.get('username') or email} ({data.get('restaurant_name') or data.get('role')})")
    return {"Authorization": f"Bearer {data['access_token']}"}


def download(client: httpx.Client, headers: dict, path: str, out: str, params: dict | None = None) -> bytes:
    response = client.get(path, headers=headers, params=params)
    if response.status_code != 200:
        _fail(response)
    if out:
        with open(out, "wb") as f:
            f.write(response.content)
        print(f"Guardado: {out}")
    return response.content


def upload(client: httpx.Client, headers: dict, path: str, content: bytes, params: dict) -> dict:
    response = client.post(path, headers=headers, params=params, files={"file": ("menu.xlsx", content, XLSX)})
    if response.status_code == 409:
        print("Se necesita confirmación (repite con --accept-warnings para continuar):")
        _fail(response)
    if response.status_code != 200:
        _fail(response)
    return response.json()


def summarize(result: dict) -> None:
    mode = "Validación OK (no se guardó nada)" if result.get("dry_run") else "Guardado"
    print(f"{mode}: {result.get('created', 0)} nuevos, {result.get('updated', 0)} actualizados.")
    if result.get("replaced_dates"):
        print(f"Fechas reemplazadas: {', '.join(result['replaced_dates'])}")
    for warning in result.get("warnings") or []:
        print(f"  Aviso: {warning.get('message')}")


def complete_workbook(content: bytes) -> tuple[bytes, list[str]]:
    """Llena solo los campos vacíos con datos de ejemplo y agrega modificadores
    a los productos que no tienen ninguno."""
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content))
    menu = workbook["menu"]
    header = [c.value for c in menu[1]]
    col = {name: header.index(name) for name in header if name}
    modifiers_sheet = workbook["modificadores"]
    with_modifiers = {row[0] for row in modifiers_sheet.iter_rows(min_row=2, values_only=True) if row and row[0]}

    changes: list[str] = []
    for row in menu.iter_rows(min_row=2):
        code = row[col["codigo"]].value
        name = (row[col["nombre"]].value or "").strip()
        category = (row[col["categoria"]].value or "").strip().lower()
        if not code or not name:
            continue
        example = EXAMPLE_BY_NAME.get(name.lower()) or EXAMPLE_BY_CATEGORY.get(category) or {}
        filled = []
        for field in ("listo_desde", "hora_limite_pedido", "porciones_dia"):
            if field in example and row[col[field]].value in (None, ""):
                row[col[field]].value = example[field]
                filled.append(f"{field}={example[field]}")
        if example.get("modifiers") and code not in with_modifiers:
            for group, mod_name, price in example["modifiers"]:
                modifiers_sheet.append([code, group, mod_name, price])
            with_modifiers.add(code)
            filled.append(f"{len(example['modifiers'])} modificadores")
        if filled:
            changes.append(f"{name} ({code}): {', '.join(filled)}")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["export", "import", "calendar-template", "calendar-export", "calendar-import", "completar"])
    parser.add_argument("file", nargs="?", help="Archivo .xlsx para import / calendar-import")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--restaurant-id", default=None, help="Solo para superadmin")
    parser.add_argument("--out", default=None, help="Archivo de salida para las descargas")
    parser.add_argument("--dry-run", action="store_true", help="Solo valida, no guarda")
    parser.add_argument("--accept-warnings", action="store_true", help="Calendario: reemplaza fechas que ya tienen menú")
    args = parser.parse_args()

    base_params = {"restaurant_id": args.restaurant_id} if args.restaurant_id else {}
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=90.0) as client:
        headers = login(client, args.email, args.password)
        today = date.today().isoformat()

        if args.command == "export":
            download(client, headers, "/api/admin/menu/export", args.out or f"menu_{today}.xlsx", base_params)
        elif args.command == "calendar-template":
            download(client, headers, "/api/admin/menu/calendar/template", args.out or "plantilla_calendario.xlsx")
        elif args.command == "calendar-export":
            download(client, headers, "/api/admin/menu/calendar/export", args.out or f"calendario_{today}.xlsx", base_params)
        elif args.command in ("import", "calendar-import"):
            if not args.file:
                parser.error("Indica el archivo .xlsx a subir.")
            try:
                with open(args.file, "rb") as f:
                    content = f.read()
            except FileNotFoundError:
                sys.exit(f"No existe el archivo '{args.file}'.")
            path = "/api/admin/menu/import" if args.command == "import" else "/api/admin/menu/calendar/import"
            params = {**base_params, "dry_run": str(args.dry_run).lower()}
            if args.command == "calendar-import":
                params["accept_warnings"] = str(args.accept_warnings).lower()
            summarize(upload(client, headers, path, content, params))
        else:  # completar
            current = download(client, headers, "/api/admin/menu/export", args.out or "", base_params)
            completed, changes = complete_workbook(current)
            if not changes:
                print("No hay campos vacíos que completar con datos de ejemplo.")
                return
            print("Cambios propuestos:")
            for line in changes:
                print(f"  - {line}")
            summarize(upload(client, headers, "/api/admin/menu/import", completed, {**base_params, "dry_run": "true"}))
            if args.dry_run:
                return
            summarize(upload(client, headers, "/api/admin/menu/import", completed, {**base_params, "dry_run": "false"}))
            with open(f"menu_completado_{today}.xlsx", "wb") as f:
                f.write(completed)
            print(f"Copia del menú completado: menu_completado_{today}.xlsx")


if __name__ == "__main__":
    try:
        main()
    except httpx.ConnectError:
        sys.exit("No se pudo conectar a la API. Revisa --base-url y que el servidor esté arriba.")
    except json.JSONDecodeError:
        sys.exit("La API respondió algo inesperado.")
