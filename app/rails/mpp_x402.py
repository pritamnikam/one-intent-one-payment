from typing import Literal

from app.models import AP2Intent

PaymentRail = Literal["mpp", "x402"]

RAIL_DESCRIPTIONS = {
    "mpp": "Machine Payments Protocol (MPP)-style payment request",
    "x402": "HTTP 402 / x402-style payment request",
}


def prepare_simulated_payment(intent: AP2Intent) -> dict[str, str]:
    """Describe the selected rail without contacting a wallet, merchant, or network."""
    return {
        "rail": intent.rail,
        "protocol": RAIL_DESCRIPTIONS[intent.rail],
        "mode": "simulation",
        "merchant": intent.merchant,
        "amount": str(intent.amount),
        "currency": intent.currency,
    }
