import hashlib
import json
import logging
import os
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.models import AP2Intent, GuardrailDecision, PaymentAuditEvent, PaymentReq
from app.rails.mpp_x402 import prepare_simulated_payment
from app.rails.stripe_payment_intents import (
    PAYMENT_PROVIDER,
    STRIPE_PAYMENT_STATUSES,
)
from app.secrets import read_secret

logger = logging.getLogger(__name__)
DATABASE_URL = os.getenv("DATABASE_URL") or make_conninfo(
    host=os.getenv("POSTGRES_HOST", "localhost"),
    port=os.getenv("POSTGRES_PORT", "5432"),
    dbname=os.getenv("POSTGRES_DB", "payments"),
    user=os.getenv("POSTGRES_USER", "demo"),
    password=read_secret("POSTGRES_PASSWORD"),
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id UUID PRIMARY KEY,
    intent_id UUID NOT NULL UNIQUE,
    owner_id TEXT,
    provider TEXT NOT NULL DEFAULT 'simulation',
    provider_payment_id TEXT,
    client_secret TEXT,
    provider_event_created_at BIGINT,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    currency CHAR(3) NOT NULL,
    merchant TEXT NOT NULL,
    rail TEXT NOT NULL CHECK (rail IN ('mpp', 'x402')),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE payments ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'simulation';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider_payment_id TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS client_secret TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider_event_created_at BIGINT;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (
    status IN (
        'pending', 'simulated', 'denied', 'failed',
        'requires_payment_method', 'requires_confirmation', 'requires_action',
        'processing', 'requires_capture', 'succeeded', 'canceled'
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS payments_provider_payment_id_idx
    ON payments (provider_payment_id) WHERE provider_payment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS payment_outbox (
    event_id UUID PRIMARY KEY,
    payment_id UUID NOT NULL REFERENCES payments(payment_id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

ALTER TABLE payment_outbox ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE payment_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;

DROP INDEX IF EXISTS payment_outbox_pending_idx;
CREATE INDEX IF NOT EXISTS payment_outbox_pending_available_idx
    ON payment_outbox (available_at, created_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    provider_payment_id TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payment_audit_events (
    audit_id UUID PRIMARY KEY,
    intent_id UUID NOT NULL,
    payment_id UUID REFERENCES payments(payment_id),
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payment_audit_intent_idx
    ON payment_audit_events (intent_id, created_at);

CREATE INDEX IF NOT EXISTS payment_audit_payment_idx
    ON payment_audit_events (payment_id, created_at)
    WHERE payment_id IS NOT NULL;

CREATE OR REPLACE FUNCTION reject_payment_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'payment_audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS payment_audit_append_only ON payment_audit_events;
CREATE TRIGGER payment_audit_append_only
BEFORE UPDATE OR DELETE ON payment_audit_events
FOR EACH ROW EXECUTE FUNCTION reject_payment_audit_mutation();

DROP TRIGGER IF EXISTS payment_audit_no_truncate ON payment_audit_events;
CREATE TRIGGER payment_audit_no_truncate
BEFORE TRUNCATE ON payment_audit_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_payment_audit_mutation();
"""


def _fingerprint(intent: AP2Intent) -> str:
    canonical = json.dumps(intent.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def initialize_database(database_url: str = DATABASE_URL) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('one-intent-one-payment-schema', 0))"
            )
            await connection.execute(SCHEMA_SQL)


async def submit_payment(
    intent: AP2Intent,
    idempotency_key: str,
    decision: GuardrailDecision,
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
                SELECT payment_id, intent_id, owner_id, provider, provider_payment_id, client_secret,
                       idempotency_key, request_fingerprint,
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
                    or existing["owner_id"] != intent.user_id
                    or existing["request_fingerprint"] != fingerprint
                ):
                    raise ValueError("Intent or idempotency key was already used for a different request.")
                await append_audit_event(
                    connection,
                    intent_id=intent.intent_id,
                    payment_id=existing["payment_id"],
                    actor_id=intent.user_id,
                    event_type="payment.intent.replayed",
                    outcome="existing_payment_returned",
                    details=_intent_audit_details(intent, idempotency_key, decision),
                )
                return _payment_from_row(existing), False

            payment = PaymentReq(
                intent_id=intent.intent_id,
                amount=intent.amount,
                currency=intent.currency,
                merchant=intent.merchant,
                rail=intent.rail,
                provider=PAYMENT_PROVIDER,
                idempotency_key=idempotency_key,
            )
            rail_payload = prepare_simulated_payment(intent)
            event_payload: dict[str, Any] = {
                "payment_id": str(payment.payment_id),
                "intent_id": str(intent.intent_id),
                "provider": PAYMENT_PROVIDER,
                "rail": intent.rail,
                "user_id": intent.user_id,
                "description": intent.description,
                "amount": str(intent.amount),
                "currency": intent.currency,
                "request": rail_payload,
            }
            await connection.execute(
                """
                INSERT INTO payments
                    (payment_id, intent_id, owner_id, provider, idempotency_key, request_fingerprint,
                     amount, currency, merchant, rail, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """,
                (
                    payment.payment_id,
                    intent.intent_id,
                    intent.user_id,
                    PAYMENT_PROVIDER,
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
            await append_audit_event(
                connection,
                intent_id=intent.intent_id,
                payment_id=payment.payment_id,
                actor_id=intent.user_id,
                event_type="payment.intent.authorized",
                outcome="queued",
                details=_intent_audit_details(intent, idempotency_key, decision),
            )
            await connection.execute(
                "SELECT pg_notify('payment_outbox', %s)",
                (str(payment.payment_id),),
            )
            return payment, True


async def record_denied_intent(
    intent: AP2Intent,
    decision: GuardrailDecision,
    idempotency_key: str,
    database_url: str = DATABASE_URL,
) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.transaction():
            await append_audit_event(
                connection,
                intent_id=intent.intent_id,
                payment_id=None,
                actor_id=intent.user_id,
                event_type="payment.intent.denied",
                outcome="denied",
                details=_intent_audit_details(intent, idempotency_key, decision),
            )


async def record_conflicting_intent(
    intent: AP2Intent,
    decision: GuardrailDecision,
    idempotency_key: str,
    database_url: str = DATABASE_URL,
) -> None:
    async with await psycopg.AsyncConnection.connect(database_url) as connection:
        async with connection.transaction():
            await append_audit_event(
                connection,
                intent_id=intent.intent_id,
                payment_id=None,
                actor_id=intent.user_id,
                event_type="payment.intent.conflict",
                outcome="idempotency_conflict",
                details=_intent_audit_details(intent, idempotency_key, decision),
            )


def _intent_audit_details(
    intent: AP2Intent,
    idempotency_key: str,
    decision: GuardrailDecision,
) -> dict[str, Any]:
    return {
        "amount": str(intent.amount),
        "currency": intent.currency,
        "rail": intent.rail,
        "idempotency_key_sha256": hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
        "guardrail_reason": decision.reason,
        "policy_ids": decision.policy_ids,
    }


async def append_audit_event(
    connection: psycopg.AsyncConnection,
    *,
    intent_id: UUID,
    payment_id: UUID | None,
    actor_id: str,
    event_type: str,
    outcome: str,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        """
        INSERT INTO payment_audit_events
            (audit_id, intent_id, payment_id, actor_id, event_type, outcome, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid4(),
            intent_id,
            payment_id,
            actor_id,
            event_type,
            outcome,
            json.dumps(details),
        ),
    )


async def get_intent_audit(
    intent_id: UUID,
    actor_id: str,
    database_url: str = DATABASE_URL,
) -> list[PaymentAuditEvent]:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            """
            SELECT a.audit_id, a.intent_id, a.payment_id, a.actor_id, a.event_type,
                   a.outcome, a.details, a.created_at
            FROM payment_audit_events AS a
            LEFT JOIN payments AS p ON p.payment_id = a.payment_id
            WHERE a.intent_id = %s AND (a.actor_id = %s OR p.owner_id = %s)
            ORDER BY a.created_at, a.audit_id
            """,
            (intent_id, actor_id, actor_id),
        )
        return [PaymentAuditEvent.model_validate(row) for row in await cursor.fetchall()]


async def get_payment_audit(
    payment_id: UUID,
    owner_id: str,
    database_url: str = DATABASE_URL,
) -> list[PaymentAuditEvent]:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            """
            SELECT a.audit_id, a.intent_id, a.payment_id, a.actor_id, a.event_type,
                   a.outcome, a.details, a.created_at
            FROM payment_audit_events AS a
            JOIN payments AS p ON p.payment_id = a.payment_id
            WHERE a.payment_id = %s AND p.owner_id = %s
            ORDER BY a.created_at, a.audit_id
            """,
            (payment_id, owner_id),
        )
        return [PaymentAuditEvent.model_validate(row) for row in await cursor.fetchall()]


async def list_failed_outbox_events(
    database_url: str = DATABASE_URL,
) -> list[dict[str, Any]]:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            """
            SELECT event_id, payment_id, attempts, last_error, created_at
            FROM payment_outbox
            WHERE status = 'failed'
            ORDER BY created_at, event_id
            """
        )
        return await cursor.fetchall()


async def recover_outbox_event(
    event_id: UUID,
    actor_id: str,
    reason: str,
    database_url: str = DATABASE_URL,
) -> UUID:
    if not actor_id.strip() or len(actor_id) > 128:
        raise ValueError("Recovery actor must contain between 1 and 128 characters.")
    if not reason.strip() or len(reason) > 500:
        raise ValueError("Recovery reason must contain between 1 and 500 characters.")

    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                UPDATE payment_outbox
                SET status = 'pending', attempts = 0, available_at = now(), last_error = NULL,
                    processed_at = NULL
                WHERE event_id = %s AND status = 'failed'
                RETURNING payment_id
                """,
                (event_id,),
            )
            event = await cursor.fetchone()
            if event is None:
                raise LookupError("Failed outbox event was not found or is no longer recoverable.")

            payment_cursor = await connection.execute(
                """
                UPDATE payments
                SET status = 'pending', client_secret = NULL
                WHERE payment_id = %s AND status = 'failed'
                RETURNING intent_id
                """,
                (event["payment_id"],),
            )
            payment = await payment_cursor.fetchone()
            if payment is None:
                raise ValueError("Outbox event payment is not in a recoverable failed state.")

            await append_audit_event(
                connection,
                intent_id=payment["intent_id"],
                payment_id=event["payment_id"],
                actor_id=actor_id,
                event_type="payment.outbox.requeued",
                outcome="operator_recovery",
                details={"reason": reason},
            )
            await connection.execute(
                "SELECT pg_notify('payment_outbox', %s)",
                (str(event_id),),
            )
            return event["payment_id"]


def _payment_from_row(row: dict[str, Any]) -> PaymentReq:
    return PaymentReq(
        payment_id=row["payment_id"],
        intent_id=row["intent_id"],
        amount=row["amount"],
        currency=row["currency"].strip(),
        merchant=row["merchant"],
        rail=row["rail"],
        provider=row["provider"],
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        client_secret=row["client_secret"],
        created_at=row["created_at"],
    )


async def get_payment(
    payment_id: UUID,
    owner_id: str,
    database_url: str = DATABASE_URL,
) -> PaymentReq | None:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        cursor = await connection.execute(
            """
            SELECT payment_id, intent_id, owner_id, provider, provider_payment_id, client_secret,
                   idempotency_key, request_fingerprint,
                   amount, currency, merchant, rail, status, created_at
            FROM payments WHERE payment_id = %s AND owner_id = %s
            """,
            (payment_id, owner_id),
        )
        row = await cursor.fetchone()
        return _payment_from_row(row) if row else None


async def store_stripe_payment_intent(
    payment_id: UUID,
    provider_payment_id: str,
    client_secret: str,
    status: str,
    database_url: str = DATABASE_URL,
) -> None:
    if status not in STRIPE_PAYMENT_STATUSES:
        raise ValueError(f"Unsupported Stripe PaymentIntent status: {status}")
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                UPDATE payments
                SET provider_payment_id = %s, client_secret = %s, status = %s
                WHERE payment_id = %s AND provider = 'stripe_test'
                RETURNING intent_id, owner_id
                """,
                (provider_payment_id, client_secret, status, payment_id),
            )
            payment = await cursor.fetchone()
            if payment is None:
                raise LookupError(f"Payment {payment_id} was not found for Stripe provider update.")
            await append_audit_event(
                connection,
                intent_id=payment["intent_id"],
                payment_id=payment_id,
                actor_id="system:stripe",
                event_type="payment.provider_intent_created",
                outcome=status,
                details={
                    "provider_payment_id": provider_payment_id,
                    "provider_status": status,
                },
            )


async def record_stripe_webhook_event(
    event_id: str,
    event_type: str,
    provider_payment_id: str | None,
    status: str | None,
    event_created_at: int,
    database_url: str = DATABASE_URL,
) -> bool:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        async with connection.transaction():
            cursor = await connection.execute(
                """
                INSERT INTO stripe_webhook_events (event_id, event_type, provider_payment_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, event_type, provider_payment_id),
            )
            if cursor.rowcount == 0:
                return False

            if status not in STRIPE_PAYMENT_STATUSES or not provider_payment_id:
                return True

            payment_cursor = await connection.execute(
                """
                UPDATE payments
                SET status = %s,
                    provider_event_created_at = %s,
                    client_secret = CASE WHEN %s IN ('succeeded', 'canceled')
                        THEN NULL ELSE client_secret END
                WHERE provider_payment_id = %s
                  AND provider = 'stripe_test'
                  AND status NOT IN ('succeeded', 'canceled')
                  AND (
                    provider_event_created_at IS NULL
                    OR provider_event_created_at <= %s
                  )
                RETURNING payment_id, intent_id
                """,
                (status, event_created_at, status, provider_payment_id, event_created_at),
            )
            payment = await payment_cursor.fetchone()
            if payment is not None:
                await append_audit_event(
                    connection,
                    intent_id=payment["intent_id"],
                    payment_id=payment["payment_id"],
                    actor_id="stripe:webhook",
                    event_type="payment.provider_status_updated",
                    outcome=status,
                    details={
                        "stripe_event_id": event_id,
                        "stripe_event_type": event_type,
                        "provider_payment_id": provider_payment_id,
                        "provider_event_created_at": event_created_at,
                    },
                )
            return True
