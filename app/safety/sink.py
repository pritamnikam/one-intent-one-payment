import asyncio
import logging
import os
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from prometheus_client import start_http_server
from psycopg.rows import dict_row

from app.metrics import OUTBOX_EVENTS, REGISTRY
from app.rails.stripe_payment_intents import (
    PAYMENT_PROVIDER,
    create_payment_intent,
    validate_payment_configuration,
)
from app.safety.outbox import (
    DATABASE_URL,
    append_audit_event,
    initialize_database,
    store_stripe_payment_intent,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("payment-safety-sink")
MAX_OUTBOX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 2
RETRY_MAX_SECONDS = 300


async def process_one(database_url: str = DATABASE_URL) -> bool:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT event_id, payment_id, payload, attempts
                FROM payment_outbox
                WHERE status = 'pending' AND available_at <= now()
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
            event = await cursor.fetchone()
            if event is None:
                return False
            try:
                await _process_payment(event["payload"], database_url)
            except Exception as error:
                attempt = event["attempts"] + 1
                dead_letter = attempt >= MAX_OUTBOX_ATTEMPTS
                retry_delay = min(RETRY_BASE_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_SECONDS)
                logger.error(
                    "Safety sink failed for outbox event %s (attempt=%s, error_type=%s).",
                    event["event_id"],
                    attempt,
                    type(error).__name__,
                )
                await connection.execute(
                    """
                    UPDATE payment_outbox
                    SET status = %s, attempts = %s,
                        available_at = now() + (%s * interval '1 second'),
                        last_error = %s
                    WHERE event_id = %s
                    """,
                    (
                        "failed" if dead_letter else "pending",
                        attempt,
                        0 if dead_letter else retry_delay,
                        type(error).__name__,
                        event["event_id"],
                    ),
                )
                if dead_letter:
                    await connection.execute(
                        "UPDATE payments SET status = 'failed', client_secret = NULL WHERE payment_id = %s",
                        (event["payment_id"],),
                    )
                payload = event["payload"]
                await append_audit_event(
                    connection,
                    intent_id=UUID(payload["intent_id"]),
                    payment_id=event["payment_id"],
                    actor_id="system:outbox",
                    event_type="payment.processing_failed" if dead_letter else "payment.processing_retry",
                    outcome="dead_letter" if dead_letter else "retry_scheduled",
                    details={
                        "attempt": attempt,
                        "error_type": type(error).__name__,
                        "retry_delay_seconds": 0 if dead_letter else retry_delay,
                    },
                )
                OUTBOX_EVENTS.labels("dead_letter" if dead_letter else "retry").inc()
                return True
            await connection.execute(
                """
                UPDATE payment_outbox
                SET status = 'processed', attempts = attempts + 1, processed_at = now(), last_error = NULL
                WHERE event_id = %s
                """,
                (event["event_id"],),
            )
            if event["payload"].get("provider", "simulation") == "simulation":
                await connection.execute(
                    "UPDATE payments SET status = 'simulated' WHERE payment_id = %s",
                    (event["payment_id"],),
                )
                payload = event["payload"]
                await append_audit_event(
                    connection,
                    intent_id=UUID(payload["intent_id"]),
                    payment_id=event["payment_id"],
                    actor_id="system:outbox",
                    event_type="payment.simulated",
                    outcome="simulated",
                    details={"provider": "simulation"},
                )
            OUTBOX_EVENTS.labels("processed").inc()
        return True


async def _process_payment(payload: dict[str, Any], database_url: str) -> None:
    provider = payload.get("provider", "simulation")
    if provider == "simulation":
        logger.info(
            "SIMULATED payment event accepted: payment_id=%s rail=%s; no funds moved",
            payload["payment_id"],
            payload["rail"],
        )
        return
    if provider != "stripe_test" or PAYMENT_PROVIDER != "stripe_test":
        raise RuntimeError("Outbox event provider does not match the configured payment provider.")

    payment_intent = await asyncio.to_thread(
        create_payment_intent,
        payload["intent_id"],
        Decimal(payload["amount"]),
        payload["currency"],
        payload["payment_id"],
    )
    await store_stripe_payment_intent(
        payment_id=UUID(payload["payment_id"]),
        provider_payment_id=payment_intent["provider_payment_id"],
        client_secret=payment_intent["client_secret"],
        status=payment_intent["status"],
        database_url=database_url,
    )
    logger.info(
        "Stripe test PaymentIntent created: payment_id=%s provider_payment_id=%s status=%s",
        payload["payment_id"],
        payment_intent["provider_payment_id"],
        payment_intent["status"],
    )


async def run_sink(database_url: str = DATABASE_URL) -> None:
    validate_payment_configuration()
    await initialize_database(database_url)
    start_http_server(
        int(os.getenv("SINK_METRICS_PORT", "9100")),
        addr="0.0.0.0",
        registry=REGISTRY,
    )
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(database_url) as listener:
                await listener.execute("LISTEN payment_outbox")
                await listener.commit()
                logger.info("Listening for payment_outbox notifications.")
                while True:
                    while await process_one(database_url):
                        pass
                    async for _notification in listener.notifies(timeout=1, stop_after=1):
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox listener disconnected; retrying in 2 seconds.")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run_sink())
