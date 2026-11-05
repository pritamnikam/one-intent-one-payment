import hashlib
import json
import logging
import os
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.models import AP2Intent, PaymentReq
from app.rails.mpp_x402 import prepare_simulated_payment

logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://demo:local-dev-password@localhost:5432/payments")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id UUID PRIMARY KEY,
    intent_id UUID NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    currency CHAR(3) NOT NULL,
    merchant TEXT NOT NULL,
    rail TEXT NOT NULL CHECK (rail IN ('mpp', 'x402')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'simulated', 'denied', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payment_outbox (
    event_id UUID PRIMARY KEY,
    payment_id UUID NOT NULL REFERENCES payments(payment_id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS payment_outbox_pending_idx
    ON payment_outbox (created_at) WHERE status = 'pending';
"""


def _fingerprint(intent: AP2Intent) -> str:
    canonical = json.dumps(intent.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def initialize_database(database_url: str = DATABASE_URL) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        await connection.execute(SCHEMA_SQL)
        await connection.commit()


async def submit_payment(
    intent: AP2Intent,
    idempotency_key: str,
    database_url: str = DATABASE_URL,
) -> tuple[PaymentReq, bool]:
    fingerprint = _fingerprint(intent)
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.transaction():
            lock_keys = sorted((str(intent.intent_id), f"idempotency:{idempotency_key}"))
            for lock_key in lock_keys:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (lock_key,),
                )
            existing_cursor = await connection.execute(
                """
                SELECT payment_id, intent_id, idempotency_key, request_fingerprint,
                       amount, currency, merchant, rail, status, created_at
                FROM payments
                WHERE idempotency_key = %s OR intent_id = %s
                """,
                (idempotency_key, intent.intent_id),
            )
            existing = await existing_cursor.fetchone()
            if existing is not None:
                if (
                    existing["idempotency_key"] != idempotency_key
                    or existing["intent_id"] != intent.intent_id
                    or existing["request_fingerprint"] != fingerprint
                ):
                    raise ValueError("Intent or idempotency key was already used for a different request.")
                return _payment_from_row(existing), False

            payment = PaymentReq(
                intent_id=intent.intent_id,
                amount=intent.amount,
                currency=intent.currency,
                merchant=intent.merchant,
                rail=intent.rail,
                idempotency_key=idempotency_key,
            )
            rail_payload = prepare_simulated_payment(intent)
            event_payload: dict[str, Any] = {
                "payment_id": str(payment.payment_id),
                "intent_id": str(intent.intent_id),
                "rail": intent.rail,
                "request": rail_payload,
            }
            await connection.execute(
                """
                INSERT INTO payments
                    (payment_id, intent_id, idempotency_key, request_fingerprint,
                     amount, currency, merchant, rail, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                (
                    payment.payment_id,
                    intent.intent_id,
                    idempotency_key,
                    fingerprint,
                    intent.amount,
                    intent.currency,
                    intent.merchant,
                    intent.rail,
                ),
            )
            await connection.execute(
                """
                INSERT INTO payment_outbox (event_id, payment_id, event_type, payload)
                VALUES (%s, %s, 'payment.requested', %s)
                """,
                (payment.payment_id, payment.payment_id, json.dumps(event_payload)),
            )
            await connection.execute(
                "SELECT pg_notify('payment_outbox', %s)",
                (str(payment.payment_id),),
            )
            return payment, True


def _payment_from_row(row: dict[str, Any]) -> PaymentReq:
    return PaymentReq(
        payment_id=row["payment_id"],
        intent_id=row["intent_id"],
        amount=row["amount"],
        currency=row["currency"].strip(),
        merchant=row["merchant"],
        rail=row["rail"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def get_payment(payment_id: UUID, database_url: str = DATABASE_URL) -> PaymentReq | None:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            """
            SELECT payment_id, intent_id, idempotency_key, request_fingerprint,
                   amount, currency, merchant, rail, status, created_at
            FROM payments WHERE payment_id = %s
            """,
            (payment_id,),
        )
        row = await cursor.fetchone()
        return _payment_from_row(row) if row else None
