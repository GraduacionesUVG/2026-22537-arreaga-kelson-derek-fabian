"""Fase 2: plantillas, importación y exportación (menú general y calendario)."""

import io
from datetime import timedelta

from openpyxl import Workbook, load_workbook

from src.core.timeutils import local_today
from src.services.menu_excel import CALENDAR_COLUMNS, MENU_COLUMNS, MODIFIER_COLUMNS


def _xlsx(sheets: dict[str, tuple[list[str], list[list]]]) -> bytes:
    wb = Workbook()
    first = True
    for name, (columns, rows) in sheets.items():
        ws = wb.active if first else wb.create_sheet(name)
        ws.title = name
        first = False
        ws.append(columns)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, url, content, headers):
    return client.post(url, files={"file": ("m.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, headers=headers)


def test_templates_download(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    for url, sheet in (("/api/admin/menu/template", "menu"), ("/api/admin/menu/calendar/template", "calendario")):
        r = client.get(url, headers=gh)
        assert r.status_code == 200
        wb = load_workbook(io.BytesIO(r.content))
        assert wb.sheetnames == [sheet, "modificadores", "instrucciones"]


def test_menu_import_dry_run_errors_and_upsert(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    bad = _xlsx({"menu": (MENU_COLUMNS, [["X-1", "Nachos", "comida", None, "abc", "fijo", None, None, 5, None, None, None, "SI", "NO"],
                                         ["X-2", "Taco", "comida", None, 10, "fecha", None, None, 5, None, None, None, "SI", "NO"]])})
    r = _upload(client, "/api/admin/menu/import", bad, gh)
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert {e["row"] for e in errors} == {2, 3}

    good = _xlsx({
        "menu": (MENU_COLUMNS, [
            ["HAMB-01", "Hamburguesa", "comida", "Nueva receta", 27, "fijo", None, None, 8, None, None, None, "SI", "NO"],
            [None, "Licuado", "bebidas", None, 15, "fijo", None, None, 3, None, None, 30, "SI", "NO"],
            ["LUN-01", "Almuerzo del lunes", "almuerzos", "Pepián", 35, "semanal", "lunes", "almuerzo", 10, "11:30", "10:00", None, "SI", "NO"],
        ]),
        "modificadores": (MODIFIER_COLUMNS, [["HAMB-01", "Extras", "Tocino", 6]]),
    })
    r = _upload(client, "/api/admin/menu/import?dry_run=true", good, gh)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2 and r.json()["updated"] == 1
    assert client.get("/api/admin/menu", headers=gh).json().__len__() == 2  # nada guardado

    r = _upload(client, "/api/admin/menu/import", good, gh)
    assert r.status_code == 200, r.text
    menu = {i["name"]: i for i in client.get("/api/admin/menu", headers=gh).json()}
    assert menu["Hamburguesa"]["price"] == 27
    assert [m["name"] for m in menu["Hamburguesa"]["modifiers"]] == ["Tocino"]
    assert menu["Almuerzo del lunes"]["schedule_type"] == "weekly" and menu["Almuerzo del lunes"]["day_of_week"] == "monday"
    assert menu["Licuado"]["code"] == "LICU-01"

    export = client.get("/api/admin/menu/export", headers=gh)
    wb = load_workbook(io.BytesIO(export.content))
    codes = [row[0] for row in wb["menu"].iter_rows(min_row=2, values_only=True)]
    assert set(codes) == {"HAMB-01", "CAFE-01", "LICU-01", "LUN-01"}


def test_calendar_import_warnings_and_replace(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    today = local_today()
    tomorrow = today + timedelta(days=1)
    fmt = lambda d: d.strftime("%d/%m/%Y")

    def cal(rows):
        return _xlsx({"calendario": (CALENDAR_COLUMNS, rows)})

    first = cal([
        [fmt(today), None, 1, None, None, "Cerdo al curry + Arroz", 40, 10, None, "11:30", None, None, "SI", None],
        [fmt(today), None, 2, None, None, "Lasaña + Ensalada", 42, 10, None, "11:30", None, None, "SI", None],
        [fmt(tomorrow), None, 1, None, None, "Pollo en crema", 40, 10, None, None, None, None, "SI", None],
    ])
    r = _upload(client, "/api/admin/menu/calendar/import", first, gh)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 3
    names = [i["name"] for i in r.json()["items"]]
    assert "Almuerzo del día - Opción 1" in names

    public = [i["name"] for i in client.get(f"/api/menus/?restaurant_id={seed['gh_id']}").json()]
    assert "Almuerzo del día - Opción 2" in public

    # El almuerzo de calendario va a gh-spazio; mezclarlo con comida pide ordenar por separado
    spazio_headers, spazio = seed["login_kds"]("gh-spazio")
    r = client.post("/api/orders/", json={"student_id": "1", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Almuerzo del día - Opción 1", "quantity": 1}]})
    assert r.status_code == 201 and r.json()["offered_kds_id"] == spazio["kds_user_id"]
    r = client.post("/api/orders/", json={"student_id": "1", "restaurant_id": seed["gh_id"], "items": [{"product_name": "Almuerzo del día - Opción 1", "quantity": 1}, {"product_name": "Café", "quantity": 1}]})
    assert r.status_code == 400 and "por separado" in r.json()["detail"]

    second = cal([
        [fmt(today), None, 1, None, None, "Bistec + Arroz", 45, 10, None, None, None, None, "SI", None],
        [fmt(today + timedelta(days=2)), None, 1, None, None, "Tilapia", 40, 10, None, None, None, None, "SI", None],
    ])
    r = _upload(client, "/api/admin/menu/calendar/import", second, gh)
    assert r.status_code == 409
    warnings = r.json()["detail"]["warnings"]
    assert warnings[0]["date"] == today.isoformat() and warnings[0]["orders_count"] == 1

    r = _upload(client, "/api/admin/menu/calendar/import?accept_warnings=true", second, gh)
    assert r.status_code == 200, r.text
    assert r.json()["replaced_dates"] == [today.isoformat()]
    cal_items = client.get("/api/admin/menu/calendar", headers=gh).json()
    today_items = [i for i in cal_items if i["serve_date"] == today.isoformat()]
    assert [i["description"] for i in today_items] == ["Bistec + Arroz"]
    assert len(cal_items) == 3

    export = client.get("/api/admin/menu/calendar/export", headers=gh)
    rows = list(load_workbook(io.BytesIO(export.content))["calendario"].iter_rows(min_row=2, values_only=True))
    assert len(rows) == 3 and rows[0][3] == "Almuerzo del día"


def test_calendar_weekday_mismatch(client, seed):
    gh = seed["login_admin"]("admin@gh.com")
    content = _xlsx({"calendario": (CALENDAR_COLUMNS, [["25/09/2026", "lunes", 1, None, None, "X", 40, 10, None, None, None, None, "SI", None]])})
    r = _upload(client, "/api/admin/menu/calendar/import", content, gh)
    assert r.status_code == 422
    assert "viernes" in r.json()["detail"]["errors"][0]["error"]


def test_export_assigns_codes_and_roundtrip(client, seed, db):
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        db.menu_items.insert_one({"restaurant_id": seed["gh_id"], "name": "Sin código", "category": "comida", "price": 9.0,
                                  "available": True, "modifiers": [{"id": "z", "name": "Extra", "price": 1.0}]})
    )
    gh = seed["login_admin"]("admin@gh.com")
    export = client.get("/api/admin/menu/export", headers=gh)
    r = _upload(client, "/api/admin/menu/import?dry_run=true", export.content, gh)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 0
