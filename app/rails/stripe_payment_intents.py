import os
from decimal import Decimal
from typing import Any

import stripe

from app.secrets import read_secret

PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "simulation")
STRIPE_SECRET_KEY = read_secret("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = read_secret("STRIPE_WEBHOOK_SECRET")
STRIPE_PAYMENT_STATUSES = frozenset(
    {
        "requires_payment_method",
        "requires_confirmation",
        "requires_action",
        "processing",
        "requires_capture",
        "succeeded",
        "canceled",
    }
)


def validate_payment_configuration() -> None:
    if PAYMENT_PROVIDER not in {"simulation", "stripe_test"}:
        raise RuntimeError("PAYMENT_PROVIDER must be 'simulation' or 'stripe_test'.")
    if PAYMENT_PROVIDER == "stripe_test":
        if not STRIPE_SECRET_KEY.startswith("sk_test_"):
            raise RuntimeError("Stripe test mode requires STRIPE_SECRET_KEY to start with sk_test_.")


def create_payment_intent(
    intent_id: str,
    amount: Decimal,
    currency: str,
    payment_id: str,
) -> dict[str, str]:
    validate_payment_configuration()
    if PAYMENT_PROVIDER != "stripe_test":
        raise RuntimeError("Stripe PaymentIntents are only available in stripe_test mode.")

    client = stripe.StripeClient(STRIPE_SECRET_KEY)
    stripe_intent = client.v1.payment_intents.create(
        {
            "amount": _minor_units(amount),
            "currency": currency.lower(),
            "automatic_payment_methods": {"enabled": True},
            "metadata": {
                "payment_id": payment_id,
                "intent_id": intent_id,
            },
        },
        options={"idempotency_key": f"one-intent-one-payment-{payment_id}"},
    )
    if not stripe_intent.client_secret:
        raise RuntimeError("Stripe created a PaymentIntent without a client secret.")
    if stripe_intent.status not in STRIPE_PAYMENT_STATUSES:
        raise RuntimeError(f"Stripe returned an unsupported PaymentIntent status: {stripe_intent.status}")

    return {
        "provider_payment_id": stripe_intent.id,
        "client_secret": stripe_intent.client_secret,
        "status": stripe_intent.status,
    }


def construct_webhook_event(payload: bytes, signature: str) -> Any:
    validate_payment_configuration()
    if PAYMENT_PROVIDER != "stripe_test":
        raise RuntimeError("Stripe webhooks are only enabled in stripe_test mode.")
    if not STRIPE_WEBHOOK_SECRET.startswith("whsec_"):
        raise RuntimeError("Set STRIPE_WEBHOOK_SECRET from the active Stripe CLI listener.")
    event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
    if event.get("livemode") is not False:
        raise ValueError("Live-mode Stripe webhook events are not accepted.")
    return event


def _minor_units(amount: Decimal) -> int:
    minor_units = amount * 100
    if minor_units != minor_units.to_integral_value():
        raise ValueError("Stripe demo amounts must have at most two decimal places.")
    return int(minor_units)
