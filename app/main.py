import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query
from qdrant_client import QdrantClient

from app.cards.ramp_agent_cards import AGENT_CARDS
from app.guardrail.gate import evaluate_intent
from app.models import AP2Intent, GuardrailDecision, PaymentSubmission, PolicySearchResult
from app.retrieval.hybrid_rrf import HybridPolicySearch, create_qdrant_client
from app.safety.outbox import (
    DATABASE_URL,
    get_payment,
    initialize_database,
    submit_payment,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")


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
    description="A local, simulation-only AP2 payment intent proof of concept.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/", tags=["discovery"])
async def root() -> dict[str, str]:
    return {
        "name": "One Intent, One Payment",
        "docs": "/docs",
        "safety": "local simulation only; no real payments are made",
    }


@app.get("/healthz", tags=["operations"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
) -> list[dict]:
    search: HybridPolicySearch = app.state.policy_search
    try:
        return await asyncio.to_thread(search.search, q, limit)
    except Exception as error:
        logger.exception("Policy retrieval failed.")
        raise HTTPException(status_code=503, detail="Policy retrieval is temporarily unavailable.") from error


@app.post("/intents/authorize", response_model=PaymentSubmission, tags=["payments"])
async def authorize_intent(
    intent: AP2Intent,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
) -> PaymentSubmission:
    policy_results = await search_policies(
        q=f"{intent.description} {intent.currency} {intent.amount} {intent.merchant}",
        limit=3,
    )
    decision: GuardrailDecision = evaluate_intent(
        intent,
        policy_ids=[result.policy_id for result in policy_results],
    )
    if not decision.allowed:
        return PaymentSubmission(intent=intent, decision=decision)

    try:
        payment, created = await submit_payment(intent, idempotency_key, DATABASE_URL)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        logger.exception("Payment intent could not be persisted.")
        raise HTTPException(status_code=503, detail="Payment request could not be queued.") from error

    if not created:
        logger.info("Returned existing payment for idempotent request %s", idempotency_key)
    return PaymentSubmission(intent=intent, decision=decision, payment=payment)


@app.get("/payments/{payment_id}", response_model=PaymentSubmission, tags=["payments"])
async def payment_status(payment_id: UUID) -> PaymentSubmission:
    try:
        payment = await get_payment(payment_id, DATABASE_URL)
    except Exception as error:
        logger.exception("Payment status lookup failed.")
        raise HTTPException(status_code=503, detail="Payment status is temporarily unavailable.") from error
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found.")
    intent = AP2Intent(
        intent_id=payment.intent_id,
        user_id="not-stored",
        merchant=payment.merchant,
        description="Intent metadata is not persisted in this demo.",
        amount=payment.amount,
        currency=payment.currency,
        rail=payment.rail,
    )
    return PaymentSubmission(
        intent=intent,
        decision=GuardrailDecision(allowed=True, reason="Previously authorized intent."),
        payment=payment,
    )
