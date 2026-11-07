import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

import stripe
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
import psycopg
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from qdrant_client import QdrantClient

from app.auth import Principal, require_scopes, validate_jwt_configuration
from app.cards.ramp_agent_cards import AGENT_CARDS
from app.guardrail.gate import evaluate_intent
from app.metrics import (
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS,
    PAYMENT_DECISIONS,
    REGISTRY,
    STRIPE_WEBHOOK_EVENTS,
)
from app.models import AP2Intent, PaymentAuditEvent, PaymentReq, PaymentSubmission, PolicySearchResult
from app.rails.stripe_payment_intents import (
    PAYMENT_PROVIDER,
    construct_webhook_event,
    validate_payment_configuration,
)
from app.retrieval.hybrid_rrf import COLLECTION_NAME, HybridPolicySearch, create_qdrant_client
from app.safety.outbox import (
    DATABASE_URL,
    get_intent_audit,
    get_payment,
    get_payment_audit,
    initialize_database,
    record_conflicting_intent,
    record_denied_intent,
    record_stripe_webhook_event,
    submit_payment,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
MAX_STRIPE_WEBHOOK_BYTES = 256 * 1024


def _prepare_retrieval(client: QdrantClient) -> HybridPolicySearch:
    search = HybridPolicySearch(client)
    for attempt in range(30):
        try:
            search.ensure_ready()
            return search
        except Exception:
            if attempt == 29:
                raise
            logger.warning("Qdrant is not ready yet; retrying (%s/30).", attempt + 1, exc_info=True)
            time.sleep(2)
    raise RuntimeError("Qdrant did not become ready.")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    validate_jwt_configuration()
    validate_payment_configuration()
    await initialize_database(DATABASE_URL)
    client = create_qdrant_client(QDRANT_URL)
    try:
        app.state.policy_search = await asyncio.to_thread(_prepare_retrieval, client)
        logger.info("API ready; Qdrant policy collection initialized.")
        yield
    finally:
        client.close()


app = FastAPI(
    title="One Intent, One Payment",
    description="A local AP2 payment intent proof of concept with simulation and Stripe test modes.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def observe_http_requests(request: Request, call_next):
    started_at = time.perf_counter()
    method = request.method
    try:
        response = await call_next(request)
    except Exception:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        HTTP_REQUESTS.labels(method, route, "500").inc()
        HTTP_REQUEST_DURATION.labels(method, route).observe(time.perf_counter() - started_at)
        raise

    route = getattr(request.scope.get("route"), "path", "unmatched")
    HTTP_REQUESTS.labels(method, route, str(response.status_code)).inc()
    HTTP_REQUEST_DURATION.labels(method, route).observe(time.perf_counter() - started_at)
    return response


@app.get("/", tags=["discovery"])
async def root() -> dict[str, str]:
    return {
        "name": "One Intent, One Payment",
        "docs": "/docs",
        "safety": "local simulation or Stripe test mode only; live payments are disabled",
    }


@app.get("/healthz", tags=["operations"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["operations"])
async def readyz() -> JSONResponse:
    dependencies: dict[str, str] = {}
    try:
        await _check_postgres()
        dependencies["postgres"] = "ok"
    except Exception:
        logger.exception("PostgreSQL readiness check failed.")
        dependencies["postgres"] = "unavailable"

    try:
        await _check_qdrant()
        dependencies["qdrant"] = "ok"
    except Exception:
        logger.exception("Qdrant readiness check failed.")
        dependencies["qdrant"] = "unavailable"

    is_ready = all(status == "ok" for status in dependencies.values())
    return JSONResponse(
        content={"status": "ready" if is_ready else "not_ready", "dependencies": dependencies},
        status_code=200 if is_ready else 503,
    )


async def _check_postgres() -> None:
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as connection:
        await connection.execute("SELECT 1")


async def _check_qdrant() -> None:
    search: HybridPolicySearch | None = getattr(app.state, "policy_search", None)
    if search is None or not await asyncio.to_thread(
        search.client.collection_exists,
        COLLECTION_NAME,
    ):
        raise RuntimeError("Policy collection is unavailable.")


@app.get("/metrics", tags=["operations"], include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/.well-known/agent.json", tags=["discovery"])
async def agent_card() -> dict:
    return AGENT_CARDS[0]


@app.get("/agent-cards", tags=["discovery"])
async def agent_cards() -> list[dict]:
    return AGENT_CARDS


@app.get("/policies/search", response_model=list[PolicySearchResult], tags=["policies"])
async def search_policies(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=5, ge=1, le=20),
    _principal: Principal = Depends(require_scopes("policies:read")),
) -> list[PolicySearchResult]:
    return await _search_policy_results(q, limit)


async def _search_policy_results(q: str, limit: int) -> list[PolicySearchResult]:
    search: HybridPolicySearch = app.state.policy_search
    try:
        results = await asyncio.to_thread(search.search, q, limit)
        return [PolicySearchResult.model_validate(result) for result in results]
    except Exception as error:
        logger.exception("Policy retrieval failed.")
        raise HTTPException(status_code=503, detail="Policy retrieval is temporarily unavailable.") from error


@app.post("/intents/authorize", response_model=PaymentSubmission, tags=["payments"])
async def authorize_intent(
    intent: AP2Intent,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    principal: Principal = Depends(require_scopes("intents:write")),
) -> PaymentSubmission:
    if principal.subject != intent.user_id:
        raise HTTPException(status_code=403, detail="Intent user_id must match the authenticated subject.")

    policy_results = await _search_policy_results(
        f"{intent.description} {intent.currency} {intent.amount} {intent.merchant}",
        3,
    )
    decision = evaluate_intent(
        intent,
        policy_ids=[result.policy_id for result in policy_results],
    )
    PAYMENT_DECISIONS.labels("allowed" if decision.allowed else "denied").inc()
    if not decision.allowed:
        try:
            await record_denied_intent(intent, decision, idempotency_key, DATABASE_URL)
        except Exception as error:
            logger.exception("Denied payment intent could not be audited.")
            raise HTTPException(status_code=503, detail="Payment decision could not be recorded.") from error
        return PaymentSubmission(intent=intent, decision=decision)

    try:
        payment, created = await submit_payment(intent, idempotency_key, decision, DATABASE_URL)
    except ValueError as error:
        try:
            await record_conflicting_intent(intent, decision, idempotency_key, DATABASE_URL)
        except Exception as audit_error:
            logger.exception("Conflicting payment intent could not be audited.")
            raise HTTPException(status_code=503, detail="Payment conflict could not be recorded.") from audit_error
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        logger.exception("Payment intent could not be persisted.")
        raise HTTPException(status_code=503, detail="Payment request could not be queued.") from error

    if not created:
        logger.info("Returned existing payment for idempotent request %s", idempotency_key)
    return PaymentSubmission(intent=intent, decision=decision, payment=payment)


@app.get("/payments/{payment_id}", response_model=PaymentReq, tags=["payments"])
async def payment_status(
    payment_id: UUID,
    principal: Principal = Depends(require_scopes("payments:read")),
) -> PaymentReq:
    try:
        payment = await get_payment(payment_id, principal.subject, DATABASE_URL)
    except Exception as error:
        logger.exception("Payment status lookup failed.")
        raise HTTPException(status_code=503, detail="Payment status is temporarily unavailable.") from error
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found.")
    return payment


@app.get("/intents/{intent_id}/audit", response_model=list[PaymentAuditEvent], tags=["audit"])
async def intent_audit(
    intent_id: UUID,
    principal: Principal = Depends(require_scopes("payments:read")),
) -> list[PaymentAuditEvent]:
    try:
        return await get_intent_audit(intent_id, principal.subject, DATABASE_URL)
    except Exception as error:
        logger.exception("Intent audit lookup failed.")
        raise HTTPException(status_code=503, detail="Intent audit is temporarily unavailable.") from error


@app.get("/payments/{payment_id}/audit", response_model=list[PaymentAuditEvent], tags=["audit"])
async def payment_audit(
    payment_id: UUID,
    principal: Principal = Depends(require_scopes("payments:read")),
) -> list[PaymentAuditEvent]:
    try:
        return await get_payment_audit(payment_id, principal.subject, DATABASE_URL)
    except Exception as error:
        logger.exception("Payment audit lookup failed.")
        raise HTTPException(status_code=503, detail="Payment audit is temporarily unavailable.") from error


@app.post("/webhooks/stripe", tags=["payments"], include_in_schema=False)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(alias="Stripe-Signature", min_length=1),
) -> dict[str, bool]:
    if PAYMENT_PROVIDER != "stripe_test":
        raise HTTPException(status_code=404, detail="Stripe webhooks are disabled.")

    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_STRIPE_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Stripe webhook payload is too large.")
        payload.extend(chunk)

    try:
        event = construct_webhook_event(bytes(payload), stripe_signature)
    except RuntimeError as error:
        logger.error("Stripe webhook configuration is incomplete: %s", error)
        raise HTTPException(status_code=503, detail="Stripe webhook is not configured.") from error
    except (ValueError, stripe.SignatureVerificationError) as error:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature or payload.") from error

    try:
        event_type = event["type"]
        event_id = event["id"]
        event_created_at = event["created"]
        payment_intent = event["data"]["object"]
        provider_payment_id = payment_intent.get("id")
        status = payment_intent.get("status")
        if not isinstance(event_id, str) or not isinstance(event_type, str):
            raise ValueError("Malformed Stripe event metadata.")
        if not isinstance(event_created_at, int):
            raise ValueError("Malformed Stripe event timestamp.")
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail="Malformed Stripe event.") from error

    if not event_type.startswith("payment_intent."):
        STRIPE_WEBHOOK_EVENTS.labels("ignored").inc()
        return {"received": True, "duplicate": False}

    try:
        created = await record_stripe_webhook_event(
            event_id=event_id,
            event_type=event_type,
            provider_payment_id=provider_payment_id if isinstance(provider_payment_id, str) else None,
            status=status if isinstance(status, str) else None,
            event_created_at=event_created_at,
            database_url=DATABASE_URL,
        )
    except Exception as error:
        logger.exception("Could not persist Stripe webhook event %s.", event_id)
        raise HTTPException(status_code=503, detail="Stripe event could not be recorded.") from error
    STRIPE_WEBHOOK_EVENTS.labels("duplicate" if not created else "recorded").inc()
    return {"received": True, "duplicate": not created}
