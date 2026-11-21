"""
Indicadores (KPIs) y series para gráficas.

Definiciones:

- Pedido exitoso: `payment_status = paid` y `pipeline_status = delivered`.
- Ventas: suma de `total_amount` de pedidos exitosos, por día (hora de Guatemala).
- Plato más pedido: unidades de pedidos exitosos, en total y por día.
- Tiempo de preparación: `ready_marked_at - preparation_started_at` (tiempo real en cocina).
- Hora pico: solicitudes creadas por hora del día. `hora_pico_global` usa todo
  el historial del alcance; `hora_pico_filtrado`, solo el rango pedido.
- Tasa de abandono: solicitudes aceptadas que terminan canceladas sin pago ÷ aceptadas.
- Recompra: usuarios con 1 pedido exitoso vs. usuarios con 2 o más.
"""

from collections import Counter, defaultdict

from src.core.timeutils import to_local
from src.models.order import OrderPaymentStatus, OrderPipelineStatus
from src.repositories.order_repo import OrderRepository


def _is_success(order: dict) -> bool:
    return (
        order.get("payment_status") == OrderPaymentStatus.PAID.value
        and order.get("pipeline_status") == OrderPipelineStatus.DELIVERED.value
    )


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(pct * (len(ordered) - 1)))))
    return round(ordered[index], 1)


def _local_hour(order: dict) -> int | None:
    created = order.get("created_at")
    return to_local(created).hour if created else None


def _peak(orders: list[dict]) -> dict | None:
    counts = Counter(h for h in (_local_hour(o) for o in orders) if h is not None)
    if not counts:
        return None
    days = len({o.get("local_date") for o in orders if o.get("local_date")}) or 1
    hour, total = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return {"hora": hour, "etiqueta": f"{hour:02d}:00-{hour:02d}:59", "pedidos": total, "promedio_por_dia": round(total / days, 2)}


def _top_dish(orders: list[dict]) -> tuple[list[dict], dict | None]:
    counter: Counter = Counter()
    for order in orders:
        for item in order.get("items", []):
            counter[item.get("product_name")] += int(item.get("quantity", 0))
    ranking = [{"nombre": name, "cantidad": qty} for name, qty in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0] or ""))]
    return ranking, (ranking[0] if ranking else None)


def compute_kpis(range_orders: list[dict], history_orders: list[dict], include_dishes: bool = True) -> dict:
    success = [o for o in range_orders if _is_success(o)]

    # Ventas
    sales_by_day: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "pedidos": 0})
    for order in success:
        day = sales_by_day[order.get("local_date")]
        day["total"] += float(order.get("total_amount", 0))
        day["pedidos"] += 1
    ventas_por_dia = [
        {"fecha": d, "total": round(v["total"], 2), "pedidos": v["pedidos"]} for d, v in sorted(sales_by_day.items())
    ]
    total_sales = round(sum(v["total"] for v in ventas_por_dia), 2)

    # Tiempo de preparación
    prep_minutes, prep_by_day = [], defaultdict(list)
    for order in success:
        start, ready = order.get("preparation_started_at"), order.get("ready_marked_at")
        if start and ready:
            minutes = (to_local(ready) - to_local(start)).total_seconds() / 60
            if minutes >= 0:
                prep_minutes.append(minutes)
                prep_by_day[order.get("local_date")].append(minutes)

    # Hora pico (demanda: todas las solicitudes)
    per_day_hour: dict[str, Counter] = defaultdict(Counter)
    totals_by_hour: Counter = Counter()
    for order in range_orders:
        hour = _local_hour(order)
        if hour is None:
            continue
        per_day_hour[order.get("local_date")][hour] += 1
        totals_by_hour[hour] += 1

    # Abandono
    accepted = [o for o in range_orders if o.get("accepted_at")]
    abandoned = [
        o
        for o in accepted
        if o.get("pipeline_status") == OrderPipelineStatus.CANCELLED.value
        and o.get("payment_status") != OrderPaymentStatus.PAID.value
    ]
    rejected = [o for o in range_orders if o.get("pipeline_status") == OrderPipelineStatus.REJECTED.value]

    # Recompra
    per_student = Counter(o.get("student_id") for o in success if o.get("student_id"))
    one = sum(1 for c in per_student.values() if c == 1)
    many = sum(1 for c in per_student.values() if c >= 2)

    result = {
        "resumen": {
            "solicitudes": len(range_orders),
            "pedidos_exitosos": len(success),
            "ticket_promedio": round(total_sales / len(success), 2) if success else 0.0,
        },
        "ventas": {"total": total_sales, "por_dia": ventas_por_dia},
        "tiempo_preparacion_min": {
            "promedio": round(sum(prep_minutes) / len(prep_minutes), 1) if prep_minutes else None,
            "mediana": _percentile(prep_minutes, 0.5),
            "p90": _percentile(prep_minutes, 0.9),
            "muestras": len(prep_minutes),
            "por_dia": [
                {"fecha": d, "promedio": round(sum(v) / len(v), 1), "muestras": len(v)} for d, v in sorted(prep_by_day.items())
            ],
        },
        "hora_pico": {
            "hora_pico_global": _peak(history_orders),
            "hora_pico_filtrado": _peak(range_orders),
            "total_por_hora": [{"hora": h, "pedidos": totals_by_hour[h]} for h in range(24) if totals_by_hour[h]],
            "serie": [
                {"fecha": d, "por_hora": [{"hora": h, "pedidos": c} for h, c in sorted(counter.items())]}
                for d, counter in sorted(per_day_hour.items())
            ],
        },
        "abandono": {
            "tasa": round(len(abandoned) / len(accepted), 4) if accepted else 0.0,
            "abandonados": len(abandoned),
            "aceptados": len(accepted),
            "rechazados_por_cocina": len(rejected),
        },
        "recompra": {
            "usuarios_1_pedido": one,
            "usuarios_2_o_mas": many,
            "total_usuarios": one + many,
            "tasa_recompra": round(many / (one + many), 4) if (one + many) else 0.0,
        },
    }
    if include_dishes:
        ranking, top = _top_dish(success)
        by_day: dict[str, list[dict]] = defaultdict(list)
        for order in success:
            by_day[order.get("local_date")].append(order)
        result["plato_mas_pedido"] = {
            "total": top,
            "ranking": ranking[:10],
            "por_dia": [{"fecha": d, **(_top_dish(v)[1] or {})} for d, v in sorted(by_day.items())],
        }
    return result


class KpiService:
    def __init__(self, order_repo: OrderRepository):
        self.order_repo = order_repo

    async def kpis(
        self,
        scope: str,
        restaurant_id: str | None,
        kds_user_id: str | None,
        from_date: str | None,
        to_date: str | None,
    ) -> dict:
        rid = None if scope == "global" else restaurant_id
        kid = kds_user_id if scope == "kds" else None
        range_orders = await self.order_repo.find_in_range(from_date, to_date, rid, kid)
        if from_date or to_date:
            history = await self.order_repo.find_in_range(None, None, rid, kid)
        else:
            history = range_orders
        data = compute_kpis(range_orders, history, include_dishes=scope != "global")
        return {
            "scope": scope,
            "restaurant_id": rid,
            "kds_user_id": kid,
            "range": {"from": from_date, "to": to_date},
            **data,
        }
