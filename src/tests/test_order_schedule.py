from datetime import datetime, timedelta, timezone

from src.core.timeutils import GUATEMALA_TZ
from src.models.restaurant import WEEKDAYS
from src.services.order_service import _compute_schedule, _ready_from_start
from src.services.station_service import busy_minutes_from

RESTAURANT = {"name": "Test", "opening_hours": {d: {"open": "07:00", "close": "21:00"} for d in WEEKDAYS}}


def local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 12, hour, minute, tzinfo=GUATEMALA_TZ).astimezone(timezone.utc)


NOW = local(10)


def test_empty_queue_ready_after_prep_time():
    start, ready, feasible = _compute_schedule(NOW, [], 15, None, RESTAURANT)
    assert start == NOW
    assert ready == NOW + timedelta(minutes=15)
    assert feasible


def test_future_scheduled_order_does_not_delay_asap():
    busy = [(local(13, 45), local(14))]
    start, ready, feasible = _compute_schedule(NOW, busy, 15, None, RESTAURANT)
    assert ready == local(10, 15)
    assert feasible
    assert busy_minutes_from(busy, NOW) == 0


def test_queue_counts_when_kitchen_is_free_not_the_sum():
    busy = [(local(9, 55), local(10, 10)), (local(10, 10), local(10, 20))]
    assert busy_minutes_from(busy, NOW) == 20
    start, ready, _ = _compute_schedule(NOW, busy, 15, None, RESTAURANT)
    assert start == local(10, 20)
    assert ready == local(10, 35)


def test_order_that_does_not_fit_in_gap_goes_after_next_block():
    busy = [(local(10, 10), local(10, 30))]
    start, ready, _ = _compute_schedule(NOW, busy, 15, None, RESTAURANT)
    assert start == local(10, 30)
    assert ready == local(10, 45)


def test_ready_from_delays_start():
    items = [{"prep_time_minutes": 8, "ready_from": "11:30"}, {"prep_time_minutes": 5, "ready_from": None}]
    available_from = _ready_from_start(NOW, items)
    assert available_from == local(11, 30)
    start, ready, feasible = _compute_schedule(NOW, [], 8, None, RESTAURANT, available_from)
    assert start == local(11, 30)
    assert ready == local(11, 38)
    assert feasible


def test_requested_time_respected_when_free():
    start, ready, feasible = _compute_schedule(NOW, [], 15, local(12), RESTAURANT)
    assert ready == local(12)
    assert start == local(11, 45)
    assert feasible


def test_requested_time_taken_moves_to_next_free_slot():
    busy = [(local(11, 40), local(12, 10))]
    start, ready, feasible = _compute_schedule(NOW, busy, 15, local(12), RESTAURANT)
    assert start == local(12, 10)
    assert ready == local(12, 25)
    assert not feasible


def test_outside_opening_hours_moves_to_next_opening():
    late = local(20, 50)
    start, ready, feasible = _compute_schedule(late, [], 15, None, RESTAURANT)
    assert start == datetime(2026, 10, 13, 7, tzinfo=GUATEMALA_TZ).astimezone(timezone.utc)
    assert not feasible
