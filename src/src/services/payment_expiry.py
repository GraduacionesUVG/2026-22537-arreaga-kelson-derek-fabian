"""Tarea de fondo: cancela las solicitudes aceptadas cuyo link de pago venció y rechaza las que el
restaurante no atendió a tiempo."""

import asyncio
import logging

from src.configuration import settings
from src.core.database import mongo
from src.repositories.counter_repo import CounterRepository
from src.repositories.kds_user_repo import KdsUserRepository
from src.repositories.menu_repo import MenuRepository
from src.repositories.order_repo import OrderRepository
from src.repositories.restaurant_repo import RestaurantRepository
from src.services.order_service import OrderService
from src.services.payment_service import PaymentService

logger = logging.getLogger("payment_expiry")


def build_order_service(db) -> OrderService:
    return OrderService(
        OrderRepository(db),
        MenuRepository(db),
        RestaurantRepository(db),
        PaymentService(RestaurantRepository(db)),
        kds_repo=KdsUserRepository(db),
        counter_repo=CounterRepository(db),
    )


async def run_payment_expiry_loop() -> None:
    interval = max(5, settings.payment.expiry_check_seconds)
    while True:
        try:
            if mongo.db is not None:
                service = build_order_service(mongo.db)
                cancelled = await service.expire_unpaid_orders()
                if cancelled:
                    logger.info("Solicitudes canceladas por link de pago vencido: %s", cancelled)
                unanswered = await service.expire_unanswered_requests()
                if unanswered:
                    logger.info("Solicitudes rechazadas porque el restaurante no respondió: %s", unanswered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error revisando links de pago vencidos.")
        await asyncio.sleep(interval)
