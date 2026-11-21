"""
Simula el flujo COMPLETO de un pedido pegándole a la API real por HTTP,
endpoint por endpoint, y además ejercita el panel de administración:

    Fase 0   -- Estado del sistema, sesiones de KDS/admin y escucha en vivo (WebSocket)
    Fase I   -- Identificación y registro del estudiante (WhatsApp + OTP)
    Fase II  -- Conversación / function calling del bot (listar, ver menú)
    Fase III -- Solicitud y pago (estimate, solicitud, oferta a la estación KDS,
                aceptación, link de pago con hora límite, webhook BAC -> orden)
    Fase IV  -- Preparación y entrega (cocina, OTP incorrecto y correcto)
    Fase V   -- Control del restaurante (eventos del día, línea de tiempo, búsqueda)
    Fase VI  -- KPIs (restaurante, estación KDS, alias dashboard)
    Fase VII -- Menú y catálogo (CRUD de lectura, plantillas, exportación e
                importación en modo validación: no modifica datos)
    Fase VIII-- Seguridad y alcance (rutas que deben rechazar sin token / otro restaurante)
    Opcionales:
      --escenarios-alternos  pago no confirmado y rechazo de la estación (crea pedidos extra)
      --ti-email/--ti-password  panel Dev/TI y KPIs generales (superadmin)

Mientras corre, el script se conecta a `/ws/kds/{restaurant_id}` (token del
KDS) y a `/ws/admin` (token del admin) y registra en el reporte los eventos
que llegan en vivo.

Cada paso se imprime en consola y queda en un reporte Markdown con el formato:

    Fase > Paso (con descripción) > Endpoint > Response

No importa `src`, se corre directo (sin `-m`):

    uv run python scripts/simulate_order_flow.py
    uv run python scripts/simulate_order_flow.py --restaurant Gitane --kds-username gitane1 --admin-email admin@gitane.com
    uv run python scripts/simulate_order_flow.py --escenarios-alternos
    uv run python scripts/simulate_order_flow.py --ti-email ti@uvg.edu.gt --ti-password ****

Por default corre contra Golden Harvest con las credenciales que crea
scripts/seed_golden_harvest_users.py (admin@goldenharvest.com, KDS gh-uvg).
El pedido se arma con productos de la cocina UVG; los almuerzos los recibe
la estación gh-spazio.

Requiere la API corriendo (docker compose up), datos de prueba
(scripts/seed_menu_test_data.py y scripts/seed_golden_harvest_users.py) y la
migración del módulo de administración (scripts/migrate_admin_module.py) si la
base es anterior a ese módulo.
"""

import argparse
import io
import json
import random
import string
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import httpx

STEP_LOG: list[dict] = []
LAST_BINARY: dict[str, bytes] = {}  # última descarga binaria por ruta (para reusar el Excel exportado)
REPORT_PATH = "order_flow_report.md"
STEP_DELAY = 0.0  # segundos de pausa después de cada llamada (--step-delay)

# Credenciales de prueba de Golden Harvest (las crea
# scripts/seed_golden_harvest_users.py). Para otro restaurante se
# sobreescriben por CLI (--restaurant, --kds-username, --admin-email...).
DEFAULT_RESTAURANT = "Golden Harvest"
DEFAULT_KDS_USERNAME = "gh-uvg"  # cocina UVG: todo menos almuerzos (gh-spazio prepara los almuerzos)
DEFAULT_KDS_PIN = "1234"
DEFAULT_ADMIN_EMAIL = "admin@goldenharvest.com"
DEFAULT_ADMIN_PASSWORD = "Password123"

# Categorías que NO cuentan como "un plato de verdad" al elegir qué pedir
# automáticamente -- ver _select_order_items(). Ajustar si el catálogo de un
# restaurante nuevo usa otro nombre de categoría para sus bebidas.
NON_FOOD_CATEGORIES = {"bebidas"}


# --------------------------------------------------------------------------
# Helpers de registro / impresión
# --------------------------------------------------------------------------


def _print_call(step: str, method: str, path: str, params: dict | None, json_body: dict | None) -> None:
    print(f"\n=== {step} ===")
    print(f"-> {method} {path}" + (f"  params={params}" if params else ""))
    if json_body is not None:
        print(f"Request body: {json.dumps(json_body, ensure_ascii=False, indent=2)}")


def _print_response(status_code: int, body) -> None:
    print(f"<- HTTP {status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2) if isinstance(body, (dict, list)) else body)


def call(
    client: httpx.Client,
    phase: str,
    step: str,
    method: str,
    path: str,
    json_body: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    critical: bool = True,
    expected_note: str | None = None,
    files: dict | None = None,
    expect_status: int | None = None,
) -> dict | list:
    """Hace la llamada HTTP, la imprime en consola y la guarda para el reporte.

    `critical=True` (default): si la respuesta es >=400, se corta la
    simulación acá (como antes) -- se usa para los pasos de los que depende
    el resto del flujo (ej. crear el pedido).

    `critical=False`: la respuesta se registra tal cual (sea 2xx, 4xx o 5xx)
    y la simulación SIGUE -- se usa para rutas que hoy son placeholders (ej.
    el dashboard de métricas responde 501 a propósito) o para pasos
    demostrativos que no deben tumbar el resto del reporte si fallan (ej. el
    flujo de registro/OTP, que es independiente de crear el pedido).
    `expected_note` se agrega al reporte para explicar por qué un status
    "raro" en ese paso es, de hecho, lo esperado.

    `expect_status`: el paso verifica que la API responda ese código (ej. 401
    sin token). Si coincide, el paso queda OK y no corta la simulación.

    `files`: subida multipart (importación de Excel). Las descargas binarias
    se registran como "<archivo .xlsx, N bytes>".
    """
    _print_call(step, method, path, params, json_body)

    response = client.request(method, path, json=json_body, params=params, headers=headers, files=files)
    content_type = response.headers.get("content-type", "")
    if "spreadsheetml" in content_type:
        body = f"<archivo .xlsx, {len(response.content)} bytes>"
        LAST_BINARY[path] = response.content
    else:
        try:
            body = response.json()
        except ValueError:
            body = response.text

    _print_response(response.status_code, body)
    if STEP_DELAY:
        time.sleep(STEP_DELAY)

    # Si el llamador pasó `expected_note`, ya sabe que este paso es un
    # placeholder/gap (puede ser 501, o incluso 200 con un detail tipo
    # "Pendiente de implementar") -- se etiqueta como tal sin importar el
    # status code. Si no, se deriva del status code como cualquier llamada normal.
    if expect_status is not None:
        tag = "ok" if response.status_code == expect_status else "error"
        critical = False
    elif expected_note:
        tag = "gap"
    else:
        tag = "ok" if response.status_code < 400 else "error"

    STEP_LOG.append(
        {
            "phase": phase,
            "step": step,
            "method": method,
            "path": path,
            "params": params,
            "request_body": json_body,
            "status_code": response.status_code,
            "response_body": body,
            "tag": tag,
            "note": expected_note or (f"Se espera HTTP {expect_status}." if expect_status else None),
        }
    )

    if response.status_code >= 400 and critical:
        print(f"\n!! '{step}' falló con HTTP {response.status_code}. Se corta la simulación acá.")
        write_report(REPORT_PATH, summary={"Resultado": f"Falló en el paso '{step}' ({method} {path})"})
        sys.exit(1)

    return body


def manual_step(phase: str, step: str, note: str) -> None:
    """Registra un paso del flujo para el que HOY NO existe ningún endpoint
    que llamar (ej. notificaciones salientes de WhatsApp, push al KDS). No
    inventa una llamada HTTP: solo deja constancia en el reporte, con el
    formato Fase > Paso > Endpoint > Response que pide el resto de pasos."""
    print(f"\n=== {step} ===")
    print(f"(!) {note}")
    STEP_LOG.append(
        {
            "phase": phase,
            "step": step,
            "method": None,
            "path": None,
            "params": None,
            "request_body": None,
            "status_code": None,
            "response_body": None,
            "tag": "gap",
            "note": note,
        }
    )


def omitted_step(phase: str, step: str, reason: str) -> None:
    """Paso opcional que se saltó a propósito (ej. no se dieron credenciales
    de KDS/admin por línea de comandos) -- distinto de un `manual_step`
    porque el endpoint SÍ existe e implementa, solo no se ejercitó en esta
    corrida."""
    print(f"\n=== {step} ===")
    print(f"(omitido) {reason}")
    STEP_LOG.append(
        {
            "phase": phase,
            "step": step,
            "method": None,
            "path": None,
            "params": None,
            "request_body": None,
            "status_code": None,
            "response_body": None,
            "tag": "omitted",
            "note": reason,
        }
    )


def implemented_step(phase: str, step: str, note: str) -> None:
    """Paso que YA está implementado (no es un hueco) pero que este script,
    al ser puramente HTTP, no puede observar directamente -- ej. un push por
    WebSocket, o un mensaje de WhatsApp que manda un proceso aparte
    (src/worker.py) de forma asíncrona. Distinto de `manual_step` (que es
    para algo que de verdad no existe todavía): acá el mecanismo sí corrió,
    solo que esta corrida no tiene cómo conectarse a un WebSocket ni leer
    Kafka para confirmarlo en el reporte."""
    print(f"\n=== {step} ===")
    print(f"(i) {note}")
    STEP_LOG.append(
        {
            "phase": phase,
            "step": step,
            "method": None,
            "path": None,
            "params": None,
            "request_body": None,
            "status_code": None,
            "response_body": None,
            "tag": "implemented",
            "note": note,
        }
    )


# --------------------------------------------------------------------------
# Reporte Markdown
# --------------------------------------------------------------------------

TAG_LABEL = {
    "ok": "✅ OK",
    "gap": "🚧 Brecha / hallazgo (ver nota)",
    "omitted": "⏭️ Omitido (falta configuración opcional)",
    "implemented": "🔌 Implementado (no observable desde este script HTTP, ver nota)",
    "error": "❌ Error inesperado",
}


def _format_call_header(entry: dict) -> str:
    if entry["method"] == "WS":
        return f"`WS {entry['path']}`"
    if entry["method"] is None:
        if entry["tag"] == "omitted":
            return "_(paso omitido en esta corrida -- ver nota abajo)_"
        if entry["tag"] == "implemented":
            return "_(sin llamada HTTP directa -- corre por WebSocket/Kafka, ver nota abajo)_"
        return "_(no aplica -- no existe endpoint todavía)_"
    qs = f"?{httpx.QueryParams(entry['params'])}" if entry["params"] else ""
    return f"`{entry['method']} {entry['path']}{qs}`"


def write_report(path: str, summary: dict) -> None:
    lines = [
        "# Reporte de simulación: flujo completo de pedido",
        "",
        f"Generado: {datetime.now(timezone.utc).isoformat()}",
        "",
        "Formato de cada paso: **Fase > Paso > Endpoint > Response**. Los pasos",
        "marcados como 🚧 son hallazgos: pueden ser endpoints que ya existen pero",
        "devuelven un placeholder (ej. HTTP 501 'Pendiente de implementar'), partes",
        "del flujo para las que todavía no existe ningún endpoint que llamar (el",
        "bloque de Endpoint dice explícitamente que no aplica), o endpoints que SÍ",
        "funcionan pero con un comportamiento que vale la pena revisar (ej. una ruta",
        "que debería exigir autenticación y hoy no lo hace) -- el detalle exacto",
        "siempre queda en la nota de ese paso.",
        "",
        "## Resumen",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- **{key}**: {value}")

    ok = sum(1 for e in STEP_LOG if e["tag"] == "ok")
    gaps = sum(1 for e in STEP_LOG if e["tag"] == "gap")
    omitted = sum(1 for e in STEP_LOG if e["tag"] == "omitted")
    implemented = sum(1 for e in STEP_LOG if e["tag"] == "implemented")
    errors = sum(1 for e in STEP_LOG if e["tag"] == "error")
    lines.append(
        f"- **Pasos**: {len(STEP_LOG)} totales -- {ok} OK, {gaps} con hallazgos/brechas, "
        f"{omitted} omitidos, {implemented} implementados pero no observables desde este script, "
        f"{errors} con error inesperado."
    )
    lines.append("")

    current_phase = None
    step_number = 0
    for entry in STEP_LOG:
        if entry["phase"] != current_phase:
            current_phase = entry["phase"]
            lines.append(f"## {current_phase}")
            lines.append("")

        step_number += 1
        lines.append(f"### Paso {step_number} -- {entry['step']}")
        lines.append("")
        lines.append(f"**Estado:** {TAG_LABEL[entry['tag']]}")
        lines.append("")
        lines.append(f"**Endpoint:** {_format_call_header(entry)}")
        lines.append("")

        if entry["request_body"] is not None:
            lines.append("**Request:**")
            lines.append("```json")
            lines.append(json.dumps(entry["request_body"], ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")

        if entry["status_code"] is not None:
            lines.append(f"**Response ({entry['status_code']}):**")
            lines.append("```json")
            body = entry["response_body"]
            lines.append(json.dumps(body, ensure_ascii=False, indent=2) if isinstance(body, (dict, list)) else str(body))
            lines.append("```")
        else:
            lines.append("**Response:**")
            lines.append(f"> {entry['note']}")

        if entry["note"] and entry["status_code"] is not None:
            lines.append("")
            lines.append(f"_Nota: {entry['note']}_")

        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\nReporte escrito en: {path}")


# --------------------------------------------------------------------------
# Datos de prueba generados para esta corrida (evita choques entre corridas)
# --------------------------------------------------------------------------


def _random_digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _new_whatsapp_number() -> str:
    return f"+502{_random_digits(8)}"


def _select_order_items(menu: list[dict]) -> tuple[dict, dict] | None:
    """Elige 2 productos del menú para armar un pedido realista.

    1. Se descartan productos no disponibles (incluye agotados) o `dine_in_only`.
    2. Se prefieren productos fijos (no "almuerzo del día" ni menús de
       calendario): esos dependen del día y suelen ir a otra estación KDS.
    3. Se prioriza comida real sobre bebidas (nunca solo bebidas si hay comida).
    4. Si el restaurante usa estaciones por categoría, los dos productos se
       eligen de la misma categoría o de comida + bebida; si la API responde
       que se deben pedir por separado, el script reintenta con un solo producto.
    """
    orderable = [item for item in menu if item.get("available", True) and not item.get("dine_in_only")]
    non_special = [item for item in orderable if not item.get("is_daily_special")]
    pool = non_special if len(non_special) >= 2 else orderable

    if len(pool) < 2:
        return None

    food = [item for item in pool if (item.get("category") or "").strip().lower() not in NON_FOOD_CATEGORIES]
    drinks = [item for item in pool if (item.get("category") or "").strip().lower() in NON_FOOD_CATEGORIES]

    if len(food) >= 2:
        return food[0], food[1]
    if len(food) == 1 and drinks:
        return food[0], drinks[0]
    if len(food) == 1:
        other = next(item for item in pool if item is not food[0])
        return food[0], other
    return pool[0], pool[1]


def _build_items(first_item: dict, second_item: dict | None) -> list[dict]:
    """Primer producto x2 (con un modificador del catálogo si tiene) y segundo x1."""
    first = {"product_name": first_item["name"], "quantity": 2}
    modifiers = first_item.get("modifiers") or []
    if modifiers:
        first["modifiers"] = [modifiers[0]["name"]]
    items = [first]
    if second_item is not None:
        items.append({"product_name": second_item["name"], "quantity": 1})
    return items


# --------------------------------------------------------------------------
# Escucha en vivo (WebSocket) durante la simulación
# --------------------------------------------------------------------------


class LiveListener:
    """Se conecta a un WebSocket en un hilo aparte y guarda lo que recibe."""

    def __init__(self, url: str, label: str):
        self.url = url
        self.label = label
        self.messages: list[dict] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        time.sleep(0.8)  # dar tiempo a que conecte antes del primer evento

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect
        except Exception as exc:  # pragma: no cover - depende del entorno
            self.error = f"No está instalado 'websockets' ({exc})."
            return
        try:
            with connect(self.url, open_timeout=10) as ws:
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    if time.monotonic() - last_ping > 25:
                        ws.send("ping")
                        last_ping = time.monotonic()
                    try:
                        raw = ws.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    if raw == "pong":
                        continue
                    try:
                        self.messages.append(json.loads(raw))
                    except ValueError:
                        self.messages.append({"raw": raw})
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        time.sleep(1.0)  # últimos eventos en camino
        self._stop.set()
        self._thread.join(timeout=3)


def live_step(phase: str, step: str, listener: LiveListener, summarize) -> None:
    """Registra en el reporte lo recibido por un WebSocket."""
    shown_url = listener.url.split("?")[0]
    print(f"\n=== {step} ===")
    if listener.error and not listener.messages:
        print(f"(!) {listener.error}")
        STEP_LOG.append(
            {
                "phase": phase, "step": step, "method": "WS", "path": shown_url, "params": None,
                "request_body": None, "status_code": None, "response_body": None, "tag": "error",
                "note": f"No se pudo conectar o se cerró la conexión: {listener.error}",
            }
        )
        return
    body = [summarize(m) for m in listener.messages]
    for line in body:
        print(f"  {line}")
    STEP_LOG.append(
        {
            "phase": phase, "step": step, "method": "WS", "path": shown_url, "params": None,
            "request_body": None, "status_code": 101, "response_body": body,
            "tag": "ok" if body else "error",
            "note": f"{len(body)} mensajes recibidos en vivo durante la simulación.",
        }
    )


def _summarize_kds(message: dict) -> dict:
    order = message.get("order") or {}
    return {
        "event": message.get("event"),
        "request_number": order.get("request_number"),
        "order_number": order.get("order_number"),
        "pipeline_status": order.get("pipeline_status"),
        "message": message.get("message"),
    }


def _summarize_admin(message: dict) -> dict:
    if message.get("type") == "backfill":
        return {"type": "backfill", "date": message.get("date"), "events": len(message.get("events") or [])}
    event = message.get("event") or {}
    return {"type": message.get("type"), "seq": event.get("seq"), "event": event.get("event"), "message": event.get("message")}


def _ws_url(base_url: str, path: str) -> str:
    return base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + path


# --------------------------------------------------------------------------
# Excel en memoria (importación en modo validación)
# --------------------------------------------------------------------------


def _calendar_workbook(serve_date: date) -> bytes | None:
    """Calendario de 2 opciones para una fecha futura (se sube con dry_run=true)."""
    try:
        from openpyxl import Workbook
    except Exception:
        return None
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "calendario"
    sheet.append(["fecha", "dia_semana", "opcion", "nombre", "categoria", "descripcion", "precio",
                  "tiempo_prep_min", "periodo", "listo_desde", "hora_limite_pedido", "porciones_dia",
                  "disponible", "codigo"])
    fecha = serve_date.strftime("%d/%m/%Y")
    sheet.append([fecha, None, 1, None, None, "Pollo en crema + Arroz + Ensalada (simulación)", 40, 10, None, "11:30", None, None, "SI", None])
    sheet.append([fecha, None, 2, None, None, "Lasaña boloñesa + Vegetales (simulación)", 42, 10, None, "11:30", None, None, "SI", None])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --------------------------------------------------------------------------
# Flujo principal
# --------------------------------------------------------------------------


def main(
    base_url: str,
    restaurant_name: str,
    report_path: str,
    kds_username: str | None,
    kds_pin: str | None,
    admin_email: str | None,
    admin_password: str | None,
    internal_api_key: str | None,
    ti_email: str | None,
    ti_password: str | None,
    alternate_scenarios: bool,
) -> None:
    global REPORT_PATH
    REPORT_PATH = report_path

    run_id = _random_digits(6)
    whatsapp_number = _new_whatsapp_number()
    student_email = f"simulacion.{run_id}@uvg.edu.gt"
    student_id = f"9999-simulacion-{run_id}"

    extra_headers = {"X-Internal-Api-Key": internal_api_key} if internal_api_key else None
    listeners: list[tuple[str, LiveListener, callable]] = []

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        # --- Fase 0: estado, sesiones y escucha en vivo --------------------------
        fase0 = "Fase 0: Estado del sistema y sesiones"
        call(client, fase0, "Se verifica que la API y Mongo estén vivas", "GET", "/health/")

        restaurants = call(client, fase0, "Se ubica el restaurante de la simulación", "GET", "/api/restaurants/",
                           params={"active_only": "true"})
        restaurant = next((r for r in restaurants if r["name"] == restaurant_name), None)
        if restaurant is None:
            print(f"\nNo se encontró el restaurante '{restaurant_name}'. ¿Corriste seed_menu_test_data.py?")
            write_report(report_path, summary={"Resultado": f"No se encontró el restaurante '{restaurant_name}'."})
            sys.exit(1)
        restaurant_id = restaurant["id"]

        kds_token = kds_user_id = None
        station_info: dict = {}
        if kds_username and kds_pin:
            kds_login = call(client, fase0, f"Personal de cocina inicia sesión en el KDS ({kds_username})", "POST",
                             "/auth/kds/login", json_body={"username": kds_username, "pin": kds_pin}, critical=False)
            if not isinstance(kds_login, dict) or "access_token" not in kds_login:
                hint = (
                    "Crea los usuarios de prueba con: docker compose exec api python -m scripts.seed_golden_harvest_users"
                    if restaurant_name == DEFAULT_RESTAURANT
                    else "Verifica --kds-username/--kds-pin."
                )
                print(f"\n!! No se pudo iniciar sesión en el KDS '{kds_username}'. {hint}")
                write_report(report_path, summary={"Resultado": f"Login KDS '{kds_username}' falló. {hint}"})
                sys.exit(1)
            kds_token = kds_login["access_token"]
            kds_user_id = kds_login["kds_user_id"]
            station_info = call(client, fase0, "El KDS consulta los datos de su estación (prioridad y productos que recibe)",
                                "GET", "/api/kds/me", headers={"Authorization": f"Bearer {kds_token}"})
        else:
            omitted_step(fase0, "Personal de cocina inicia sesión en el KDS",
                         "No se pasaron --kds-username/--kds-pin. Aceptar y preparar el pedido requiere el token "
                         "del KDS, así que la simulación termina después de crear la solicitud.")
        kds_headers = {"Authorization": f"Bearer {kds_token}"} if kds_token else None

        admin_token = None
        if admin_email and admin_password:
            admin_login = call(client, fase0, f"Administración del restaurante inicia sesión ({admin_email})", "POST",
                               "/auth/admin/login", json_body={"email": admin_email, "password": admin_password},
                               critical=False)
            admin_token = admin_login.get("access_token") if isinstance(admin_login, dict) else None
        else:
            omitted_step(fase0, "Administración inicia sesión",
                         "No se pasaron --admin-email/--admin-password. Se omiten las fases de control, KPIs y menú.")
        admin_headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else None

        if kds_token:
            kds_listener = LiveListener(_ws_url(base_url, f"/ws/kds/{restaurant_id}?token={kds_token}"), "kds")
            kds_listener.start()
            listeners.append(("La pantalla del KDS recibe los eventos en vivo (WebSocket)", kds_listener, _summarize_kds))
        if admin_token:
            admin_listener = LiveListener(_ws_url(base_url, f"/ws/admin?token={admin_token}"), "admin")
            admin_listener.start()
            listeners.append(("La consola del panel de administración recibe el log en vivo (WebSocket)", admin_listener, _summarize_admin))

        # --- Fase I: Identificación y registro del estudiante -------------------
        fase1 = "Fase I: Identificación y registro del estudiante"

        register_resp = call(
            client,
            fase1,
            f"Estudiante se registra con su WhatsApp ({whatsapp_number}) y correo institucional ({student_email})",
            "POST",
            "/auth/register",
            json_body={
                "whatsapp_number": whatsapp_number,
                "full_name": "Estudiante Simulado",
                "email": student_email,
                "student_id": student_id,
            },
            critical=False,
        )

        debug_otp_code = register_resp.get("debug_otp_code") if isinstance(register_resp, dict) else None
        if debug_otp_code:
            call(client, fase1, f"Estudiante escribe el código OTP que le llegó al correo ({debug_otp_code})", "POST",
                 "/auth/verify-otp", json_body={"whatsapp_number": whatsapp_number, "otp_code": debug_otp_code},
                 critical=False)
            call(client, fase1, "El bot confirma el registro antes de contestar cualquier mensaje", "POST",
                 "/auth/whatsapp/check", json_body={"whatsapp_number": whatsapp_number}, headers=extra_headers,
                 critical=False)
        else:
            omitted_step(
                fase1,
                "Estudiante escribe el código OTP que le llegó al correo",
                "POST /auth/register no devolvió 'debug_otp_code' (solo con ENVIRONMENT=DEVELOPMENT). "
                "El resto del flujo de pedido no depende de esto.",
            )

        # --- Fase II: Conversación / function calling del bot --------------------
        fase2 = "Fase II: Conversación con el asistente (function calling)"

        call(client, fase2, "Estudiante pregunta qué restaurantes están disponibles", "GET", "/api/restaurants/",
             params={"active_only": "true"})
        menu = call(client, fase2, f"Estudiante pregunta qué hay para comer hoy en '{restaurant_name}'", "GET",
                    "/api/menus/", params={"restaurant_id": restaurant_id})

        selection = _select_order_items(menu)
        if selection is None:
            print(f"\n'{restaurant_name}' no tiene suficientes productos ordenables para simular un pedido.")
            write_report(report_path, summary={"Resultado": f"'{restaurant_name}' no tiene catálogo suficiente."})
            sys.exit(1)
        first_item, second_item = selection
        order_items = _build_items(first_item, second_item)

        # --- Fase III: Solicitud y pago ---------------------------------------------
        fase3 = "Fase III: Solicitud y pago"

        estimate = call(client, fase3, "Bot calcula el tiempo y costo estimado ANTES de que el estudiante confirme",
                        "POST", "/api/orders/estimate", json_body={"restaurant_id": restaurant_id, "items": order_items},
                        critical=False)
        if isinstance(estimate, dict) and "separado" in str(estimate.get("detail", "")):
            order_items = _build_items(first_item, None)
            call(client, fase3, "Los productos van a estaciones distintas: el bot vuelve a estimar con un solo producto",
                 "POST", "/api/orders/estimate", json_body={"restaurant_id": restaurant_id, "items": order_items})

        described = ", ".join(f"{i['product_name']} x{i['quantity']}" for i in order_items)
        order = call(client, fase3, f"Estudiante confirma la solicitud: {described}", "POST", "/api/orders/",
                     json_body={"student_id": student_id, "restaurant_id": restaurant_id, "items": order_items})
        order_id = order["id"]
        request_number = order.get("request_number")

        if not kds_headers:
            write_report(report_path, summary={
                "Restaurante": f"{restaurant_name} (id={restaurant_id})",
                "Solicitud": f"#{request_number} ({order_id})",
                "Resultado": "Solicitud creada. Sin credenciales de KDS no se puede continuar el flujo.",
            })
            return

        offered = order.get("offered_kds_id")
        if offered and offered != kds_user_id:
            offered_name = offered
            if admin_headers:
                stations = client.get("/api/admin/kds-users", headers=admin_headers)
                if stations.status_code == 200:
                    offered_name = next(
                        (s["username"] for s in stations.json() if s["kds_user_id"] == offered), offered
                    )
            manual_step(
                fase3,
                "La solicitud se ofreció a otra estación",
                f"La solicitud #{request_number} se ofreció a la estación '{offered_name}', no a '{kds_username}'. "
                f"Repite con --kds-username {offered_name}, o pausa esa estación desde el panel "
                "(PUT /api/admin/kds-users/{id}/active) o con seed_golden_harvest_users --pausar-otras. "
                "Corre la simulación con el usuario KDS de la estación que cubre estos productos "
                "(ver GET /api/admin/kds-users). En Golden Harvest: gh-uvg recibe comida, sushi, bebidas "
                "y cenas; gh-spazio recibe los almuerzos (scripts/seed_golden_harvest_users.py).",
            )
            write_report(report_path, summary={"Resultado": "La solicitud se asignó a otra estación KDS."})
            sys.exit(1)

        call(client, fase3, "El KDS carga su cola: la solicitud aparece como pendiente de aceptar", "GET",
             "/api/orders/active", headers=kds_headers)

        accepted = call(client, fase3, "KDS acepta la solicitud (se genera el link de pago con hora límite)", "PUT",
                        f"/api/orders/{order_id}/status", json_body={"pipeline_status": "accepted"}, headers=kds_headers)

        implemented_step(
            fase3,
            "Se le avisa al estudiante por WhatsApp que ya puede pagar",
            f"El evento 'order_accepted' dispara el mensaje con el link de pago y la hora límite "
            f"({accepted.get('payment_expires_at')}). Lo envía src/worker.py (requiere Kafka habilitado y "
            "credenciales de WhatsApp). Cuando se envía, queda registrado como 'customer_notified' en el log.",
        )

        call(client, fase3, "El bot consulta el estado de la solicitud (incluye número y hora límite de pago)", "GET",
             f"/api/orders/{order_id}/status")

        manual_step(
            fase3,
            "Estudiante paga en la interfaz web de BAC",
            "No hay ambiente sandbox de BAC todavía. El pago se simula con el webhook del siguiente paso.",
        )

        payment = call(client, fase3, "Webhook de pago simulado (BAC) confirma el pago: la solicitud se convierte en orden",
                       "POST", "/webhooks/bac-payment", json_body={"order_id": order_id, "status": "success"})
        paid = payment["order"]
        otp_code = paid["otp_code"]
        ready_at = paid["ready_at"]
        order_number = paid.get("order_number")

        # --- Fase IV: Preparación y entrega ----------------------------------------
        fase4 = "Fase IV: Preparación y entrega"

        call(client, fase4, f"Cocina marca la orden #{order_number} en preparación", "PUT",
             f"/api/orders/{order_id}/status", json_body={"pipeline_status": "in_preparation"}, headers=kds_headers)
        call(client, fase4, f"Cocina marca la orden #{order_number} lista para recoger", "PUT",
             f"/api/orders/{order_id}/status", json_body={"pipeline_status": "ready_for_pickup"}, headers=kds_headers)

        implemented_step(
            fase4,
            "Se le avisa al estudiante por WhatsApp que ya puede pasar a recoger",
            "El evento 'order_ready' dispara el mensaje con el código de mostrador (src/worker.py).",
        )

        wrong_otp = "0000" if otp_code != "0000" else "1111"
        call(client, fase4, f"En mostrador se escribe un código incorrecto ({wrong_otp}): se rechaza y queda en el log",
             "POST", f"/api/orders/{order_id}/validate-otp", json_body={"otp_code": wrong_otp}, headers=kds_headers,
             expect_status=400)
        call(client, fase4, f"Estudiante valida el código correcto ({otp_code}): orden entregada", "POST",
             f"/api/orders/{order_id}/validate-otp", json_body={"otp_code": otp_code}, headers=kds_headers)
        call(client, fase4, "Verificación: la orden entregada ya no aparece en la cola del KDS", "GET",
             "/api/orders/active", headers=kds_headers, critical=False)

        # --- Escenarios alternos (opcional) -------------------------------------------
        if alternate_scenarios:
            fase_alt = "Escenarios alternos"
            single = _build_items(first_item, None)
            failed = call(client, fase_alt, "Pago no confirmado: estudiante hace otra solicitud", "POST", "/api/orders/",
                          json_body={"student_id": student_id, "restaurant_id": restaurant_id, "items": single})
            call(client, fase_alt, "KDS la acepta", "PUT", f"/api/orders/{failed['id']}/status",
                 json_body={"pipeline_status": "accepted"}, headers=kds_headers)
            call(client, fase_alt, "BAC informa pago fallido: la solicitud se cancela (cuenta como abandono)", "POST",
                 "/webhooks/bac-payment", json_body={"order_id": failed["id"], "status": "failed"})

            rejected = call(client, fase_alt, "Rechazo en cocina: estudiante hace otra solicitud", "POST", "/api/orders/",
                            json_body={"student_id": student_id, "restaurant_id": restaurant_id, "items": single})
            result = call(client, fase_alt, "KDS rechaza la solicitud (pasa a otra estación o se rechaza si no hay más)",
                          "PUT", f"/api/orders/{rejected['id']}/status", json_body={"pipeline_status": "rejected"},
                          headers=kds_headers)
            if isinstance(result, dict) and result.get("pipeline_status") == "pending_acceptance":
                manual_step(fase_alt, "La solicitud quedó en otra estación",
                            f"Se ofreció a la estación {result.get('offered_kds_id')}; queda pendiente de que esa "
                            "estación la acepte o rechace.")

        # --- Fase V: Control del restaurante --------------------------------------------
        if admin_headers:
            fase5 = "Fase V: Control del restaurante (panel de administración)"
            call(client, fase5, "El admin confirma su sesión y rol", "GET", "/auth/admin/me", headers=admin_headers)
            call(client, fase5, "El admin ve los datos del restaurante (horario, ubicación, abierto ahora)", "GET",
                 "/api/admin/restaurant", headers=admin_headers)
            call(client, fase5, "El admin revisa qué estaciones KDS están conectadas", "GET",
                 "/api/admin/kds-users/status", headers=admin_headers)
            call(client, fase5, "El admin abre el log de eventos de hoy (live indica si conectar el WebSocket)", "GET",
                 "/api/admin/events", headers=admin_headers)
            if kds_user_id:
                call(client, fase5, f"El admin filtra el log por la estación '{kds_username}'", "GET", "/api/admin/events",
                     params={"kds_user_id": kds_user_id}, headers=admin_headers)
            call(client, fase5, "El admin lista las solicitudes/órdenes de hoy", "GET", "/api/admin/orders",
                 headers=admin_headers)
            call(client, fase5, f"El admin busca la orden #{order_number}", "GET", "/api/admin/orders/lookup",
                 params={"order_number": order_number}, headers=admin_headers)
            call(client, fase5, f"El admin abre la línea de tiempo completa de la solicitud #{request_number}", "GET",
                 f"/api/admin/orders/{order_id}/timeline", headers=admin_headers)

            # --- Fase VI: KPIs ------------------------------------------------------------
            fase6 = "Fase VI: KPIs"
            call(client, fase6, "KPIs del restaurante (todo el historial)", "GET", "/api/metrics/kpis",
                 headers=admin_headers)
            today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-6))).date()
            call(client, fase6, "KPIs de los últimos 7 días (hora pico filtrada vs global)", "GET", "/api/metrics/kpis",
                 params={"from": (today - timedelta(days=6)).isoformat(), "to": today.isoformat()}, headers=admin_headers)
            if kds_user_id:
                call(client, fase6, f"KPIs de la estación '{kds_username}'", "GET", "/api/metrics/kpis",
                     params={"scope": "kds", "kds_user_id": kds_user_id}, headers=admin_headers)
            call(client, fase6, "Alias del dashboard (scope restaurante)", "GET", "/api/metrics/dashboard",
                 headers=admin_headers)

            # --- Fase VII: Menú y catálogo -----------------------------------------------------
            fase7 = "Fase VII: Menú y catálogo (sin modificar datos)"
            call(client, fase7, "El admin lista el menú", "GET", "/api/admin/menu", headers=admin_headers)
            call(client, fase7, "El admin consulta las categorías (para asignar estaciones KDS)", "GET",
                 "/api/admin/menu/categories", headers=admin_headers)
            call(client, fase7, "El admin consulta el calendario desde hoy", "GET", "/api/admin/menu/calendar",
                 headers=admin_headers)
            call(client, fase7, "Descarga de la plantilla del menú general", "GET", "/api/admin/menu/template",
                 headers=admin_headers)
            call(client, fase7, "Descarga de la plantilla del calendario", "GET", "/api/admin/menu/calendar/template",
                 headers=admin_headers)
            call(client, fase7, "Descarga del menú general actual", "GET", "/api/admin/menu/export", headers=admin_headers)
            call(client, fase7, "Descarga del calendario actual", "GET", "/api/admin/menu/calendar/export",
                 headers=admin_headers)

            exported = LAST_BINARY.get("/api/admin/menu/export")
            if exported:
                call(client, fase7, "Se vuelve a subir el menú exportado en modo validación (dry_run, no guarda)", "POST",
                     "/api/admin/menu/import", params={"dry_run": "true"}, headers=admin_headers,
                     files={"file": ("menu.xlsx", exported, XLSX_TYPE)}, critical=False)
            calendar_file = _calendar_workbook(today + timedelta(days=60))
            if calendar_file:
                call(client, fase7, "Se sube un calendario de prueba en modo validación (dry_run, no guarda)", "POST",
                     "/api/admin/menu/calendar/import", params={"dry_run": "true"}, headers=admin_headers,
                     files={"file": ("calendario.xlsx", calendar_file, XLSX_TYPE)}, critical=False)
            else:
                omitted_step(fase7, "Se sube un calendario de prueba en modo validación",
                             "openpyxl no está instalado en el entorno donde corre el script.")

        # --- Fase VIII: Seguridad y alcance ---------------------------------------------------
        fase8 = "Fase VIII: Seguridad y alcance"
        lunch = next((i for i in menu if i.get("is_daily_special") and i.get("available", True)), None)
        station_categories = {c.lower() for c in (station_info.get("menu_filter") or {}).get("categories", [])}
        if (
            lunch
            and (station_info.get("menu_filter") or {}).get("mode") == "custom"
            and (lunch.get("category") or "").lower() not in station_categories
        ):
            call(client, fase8, f"Un pedido que mezcla '{lunch['name']}' (cocina de almuerzos) con '{first_item['name']}' "
                 "se rechaza pidiendo ordenar por separado", "POST", "/api/orders/estimate",
                 json_body={"restaurant_id": restaurant_id, "items": [
                     {"product_name": lunch["name"], "quantity": 1}, {"product_name": first_item["name"], "quantity": 1}]},
                 expect_status=400)
        call(client, fase8, "La cola del KDS sin token se rechaza", "GET", "/api/orders/active", expect_status=401)
        call(client, fase8, "Cambiar el estado de un pedido sin token se rechaza", "PUT",
             f"/api/orders/{order_id}/status", json_body={"pipeline_status": "in_preparation"}, expect_status=401)
        call(client, fase8, "El panel de administración sin token se rechaza", "GET", "/api/admin/restaurant",
             expect_status=401)
        if kds_headers:
            call(client, fase8, "Un token de KDS no puede usar el panel de administración", "GET",
                 "/api/admin/restaurant", headers=kds_headers, expect_status=403)
        if admin_headers:
            other = next((r for r in restaurants if r["id"] != restaurant_id), None)
            if other:
                call(client, fase8, f"El admin no puede ver datos de otro restaurante ({other['name']})", "GET",
                     "/api/admin/restaurant", params={"restaurant_id": other["id"]}, headers=admin_headers,
                     expect_status=403)
            call(client, fase8, "El admin de un restaurante no puede ver KPIs generales", "GET", "/api/metrics/kpis",
                 params={"scope": "global"}, headers=admin_headers, expect_status=403)

        # --- Dev/TI (opcional) -----------------------------------------------------------------
        if ti_email and ti_password:
            fase_ti = "Panel Dev/TI (superadmin)"
            ti_login = call(client, fase_ti, f"Dev/TI inicia sesión ({ti_email})", "POST", "/auth/admin/login",
                            json_body={"email": ti_email, "password": ti_password}, critical=False)
            ti_token = ti_login.get("access_token") if isinstance(ti_login, dict) else None
            if ti_token:
                ti_headers = {"Authorization": f"Bearer {ti_token}"}
                call(client, fase_ti, "Listado de restaurantes", "GET", "/api/ti/restaurants", headers=ti_headers)
                call(client, fase_ti, "Usuarios administradores de restaurantes", "GET", "/api/ti/restaurant-users",
                     headers=ti_headers)
                call(client, fase_ti, "Estaciones KDS de todos los restaurantes", "GET", "/api/ti/kds-users",
                     headers=ti_headers)
                call(client, fase_ti, "Consola general de eventos de hoy", "GET", "/api/ti/events", headers=ti_headers)
                call(client, fase_ti, "KPIs generales (sin plato más pedido)", "GET", "/api/metrics/kpis",
                     params={"scope": "global"}, headers=ti_headers)
                call(client, fase_ti, f"Dev/TI revisa el log de '{restaurant_name}' desde el panel del restaurante",
                     "GET", "/api/admin/events", params={"restaurant_id": restaurant_id}, headers=ti_headers)

        # --- Eventos recibidos en vivo -------------------------------------------------------------
        fase_live = "Tiempo real: eventos recibidos por WebSocket"
        if not listeners:
            omitted_step(fase_live, "Escucha en vivo", "Sin sesiones de KDS/admin no se abre ningún WebSocket.")
        for step, listener, summarize in listeners:
            listener.stop()
            live_step(fase_live, step, listener, summarize)

    write_report(
        report_path,
        summary={
            "Restaurante": f"{restaurant_name} (id={restaurant_id})",
            "Solicitud / Orden": f"Solicitud #{request_number} -> Orden #{order_number} ({order_id})",
            "Total": f"Q{order['total_amount']:.2f}",
            "Hora límite de pago": accepted.get("payment_expires_at"),
            "Listo a las (ready_at)": ready_at,
            "OTP usado": otp_code,
            "Resultado": "Flujo completo simulado (pending_acceptance -> delivered) más control, KPIs y menú.",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--restaurant", default=DEFAULT_RESTAURANT)
    parser.add_argument("--report", default="order_flow_report.md")
    parser.add_argument(
        "--kds-username",
        default=DEFAULT_KDS_USERNAME,
        help=f"Usuario KDS de la estación que recibe los productos pedidos (default: '{DEFAULT_KDS_USERNAME}'). "
        "Pasar --kds-username '' para omitir.",
    )
    parser.add_argument("--kds-pin", default=DEFAULT_KDS_PIN, help="PIN del usuario KDS (4-8 dígitos).")
    parser.add_argument(
        "--admin-email",
        default=DEFAULT_ADMIN_EMAIL,
        help=f"Correo del admin del restaurante (default: '{DEFAULT_ADMIN_EMAIL}'). Pasar --admin-email '' para omitir.",
    )
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD, help="Contraseña del admin del restaurante.")
    parser.add_argument("--ti-email", default=None, help="Correo de un superadmin (Dev/TI) para ejercitar /api/ti.")
    parser.add_argument("--ti-password", default=None, help="Contraseña del superadmin.")
    parser.add_argument(
        "--escenarios-alternos",
        action="store_true",
        help="Agrega un pago no confirmado y un rechazo de cocina (crea 2 solicitudes extra en la base).",
    )
    parser.add_argument(
        "--step-delay",
        type=float,
        default=0.0,
        help="Segundos de pausa después de cada llamada (ej. --step-delay 2 para seguirlo en el panel).",
    )
    parser.add_argument(
        "--internal-api-key",
        default=None,
        help="Valor de INTERNAL_API_KEY si está configurado en .env (header X-Internal-Api-Key)",
    )
    args = parser.parse_args()
    STEP_DELAY = args.step_delay
    main(
        args.base_url,
        args.restaurant,
        args.report,
        args.kds_username,
        args.kds_pin,
        args.admin_email,
        args.admin_password,
        args.internal_api_key,
        args.ti_email,
        args.ti_password,
        args.escenarios_alternos,
    )
