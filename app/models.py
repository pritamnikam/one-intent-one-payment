from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class AP2Intent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    intent_id: UUID
    user_id: str = Field(min_length=1, max_length=128)
    merchant: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    max_amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    rail: Literal["mpp", "x402"] = "x402"


class PaymentReq(BaseModel):
    payment_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    amount: Decimal
    currency: str
    merchant: str
    rail: Literal["mpp", "x402"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "simulated", "denied", "failed"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GuardrailDecision(BaseModel):
    allowed: bool
    reason: str
    policy_ids: list[str] = Field(default_factory=list)


class PaymentSubmission(BaseModel):
    intent: AP2Intent
    decision: GuardrailDecision
    payment: PaymentReq | None = None


class PolicySearchResult(BaseModel):
    policy_id: str
    text: str
    score: float
    sources: list[str]
