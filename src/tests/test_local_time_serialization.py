"""Las horas de pedidos salen de la API en hora de Guatemala (-06:00), no en UTC.

El bot conversacional lee estas respuestas tal cual; con "...Z" decía horas
corridas 6 horas al contestar "¿cómo va mi pedido?" o "¿a qué hora está listo?".
"""

from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder

from src.core.timeutils import format_local_12h
from src.schemas.order import OrderResponse, OrderStatusResponse, TimeEstimateResponse

READY_UTC = datetime(2026, 10, 13, 20, 5, tzinfo=timezone.utc)  # 2:05 pm en Guatemala
EXPIRES_NAIVE_UTC = datetime(2026, 10, 13, 19, 40)  # naive (como puede venir de Mongo) = UTC


def test_status_response_serializes_in_guatemala_time():
    status = OrderStatusResponse(
        id="o1",
        pipeline_status="accepted",
        payment_status="pending",
        ready_at=READY_UTC,
        payment_expires_at=EXPIRES_NAIVE_UTC,
    )
    data = jsonable_encoder(status)
    assert data["ready_at"] == "2026-10-13T14:05:00-06:00"
    assert data["payment_expires_at"] == "2026-10-13T13:40:00-06:00"
    assert data["ready_at_guatemala"] == "2:05 pm"
    assert data["payment_expires_at_guatemala"] == "1:40 pm"
    # Mismo instante: un cliente que parsee ISO obtiene la misma hora UTC.
    assert datetime.fromisoformat(data["ready_at"]) == READY_UTC


def test_status_response_without_times():
    data = jsonable_encoder(OrderStatusResponse(id="o1", pipeline_status="pending_acceptance", payment_status="pending"))
    assert data["ready_at"] is None
    assert data["ready_at_guatemala"] is None
    assert data["payment_expires_at_guatemala"] is None


def test_estimate_response_serializes_in_guatemala_time():
    estimate = TimeEstimateResponse(
        restaurant_id="r1",
        total_amount=10,
        order_prep_minutes=10,
        queue_backlog_minutes=0,
        prep_start_time=datetime(2026, 10, 13, 19, 55, tzinfo=timezone.utc),
        ready_at=READY_UTC,
        feasible=True,
    )
    data = jsonable_encoder(estimate)
    assert data["prep_start_time"] == "2026-10-13T13:55:00-06:00"
    assert data["ready_at"] == "2026-10-13T14:05:00-06:00"
    assert data["ready_at_guatemala"] == "2:05 pm"


def test_order_response_serializes_in_guatemala_time():
    order = OrderResponse(
        id="o1",
        student_id="s1",
        restaurant_id="r1",
        items=[],
        pipeline_status="accepted",
        payment_status="pending",
        ready_at=READY_UTC,
        payment_expires_at=EXPIRES_NAIVE_UTC,
        created_at=datetime(2026, 10, 13, 19, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 13, 19, 31, tzinfo=timezone.utc),
    )
    data = jsonable_encoder(order)
    assert data["ready_at"] == "2026-10-13T14:05:00-06:00"
    assert data["created_at"] == "2026-10-13T13:30:00-06:00"
    assert data["ready_at_guatemala"] == "2:05 pm"
    assert data["payment_expires_at_guatemala"] == "1:40 pm"


def test_format_local_12h_crosses_midnight_utc():
    # 01:00 UTC del 14/10 = 7:00 pm del 13/10 en Guatemala.
    assert format_local_12h(datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)) == "7:00 pm"
    assert format_local_12h(None) is None
