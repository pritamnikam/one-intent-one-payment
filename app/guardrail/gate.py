from decimal import Decimal

from app.models import AP2Intent, GuardrailDecision

MAX_PAYMENT_AMOUNT = Decimal("100.00")
SUPPORTED_CURRENCIES = frozenset({"USD", "EUR"})


def evaluate_intent(intent: AP2Intent, policy_ids: list[str] | None = None) -> GuardrailDecision:
    if intent.currency not in SUPPORTED_CURRENCIES:
        return GuardrailDecision(
            allowed=False,
            reason=f"Currency {intent.currency} is not enabled in this demo.",
            policy_ids=policy_ids or [],
        )

    if intent.amount > MAX_PAYMENT_AMOUNT:
        return GuardrailDecision(
            allowed=False,
            reason=f"Amount exceeds the demo ceiling of {MAX_PAYMENT_AMOUNT} {intent.currency}.",
            policy_ids=policy_ids or [],
        )

    if intent.max_amount is not None and intent.amount > intent.max_amount:
        return GuardrailDecision(
            allowed=False,
            reason="Amount exceeds the maximum declared in the payment intent.",
            policy_ids=policy_ids or [],
        )

    return GuardrailDecision(
        allowed=True,
        reason="Intent passed the deterministic demo guardrails.",
        policy_ids=policy_ids or [],
    )
