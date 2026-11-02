import asyncio
import logging
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.safety.outbox import DATABASE_URL, initialize_database

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("payment-safety-sink")


async def process_one(database_url: str = DATABASE_URL) -> bool:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                SELECT event_id, payment_id, payload
                FROM payment_outbox
                WHERE status = 'pending'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
            event = await cursor.fetchone()
            if event is None:
                return False
            await connection.execute(
                """
                UPDATE payment_outbox
                SET status = 'processing', attempts = attempts + 1
                WHERE event_id = %s
                """,
                (event["event_id"],),
            )

        try:
            await _simulate_sink(event["payload"])
        except Exception:
            logger.exception("Safety sink failed for outbox event %s", event["event_id"])
            async with connection.transaction():
                await connection.execute(
                    "UPDATE payment_outbox SET status = 'failed' WHERE event_id = %s",
                    (event["event_id"],),
                )
            raise

        async with connection.transaction():
            await connection.execute(
                """
                UPDATE payment_outbox
                SET status = 'processed', processed_at = now()
                WHERE event_id = %s
                """,
                (event["event_id"],),
            )
            await connection.execute(
                "UPDATE payments SET status = 'simulated' WHERE payment_id = %s",
                (event["payment_id"],),
            )
        return True


async def _simulate_sink(payload: dict[str, Any]) -> None:
    logger.info(
        "SIMULATED payment event accepted: payment_id=%s rail=%s; no funds moved",
        payload.get("payment_id"),
        payload.get("rail"),
    )


async def run_sink(database_url: str = DATABASE_URL) -> None:
    await initialize_database(database_url)
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(database_url) as listener:
                await listener.execute("LISTEN payment_outbox")
                logger.info("Listening for payment_outbox notifications.")
                while True:
                    while await process_one(database_url):
                        pass
                    async for _notification in listener.notifies():
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbox listener disconnected; retrying in 2 seconds.")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run_sink())
