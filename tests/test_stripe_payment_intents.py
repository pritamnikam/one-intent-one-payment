import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.rails import stripe_payment_intents


class StripePaymentIntentTests(unittest.TestCase):
    def test_creates_test_payment_intent_with_stable_idempotency_key(self) -> None:
        client = Mock()
        client.v1.payment_intents.create.return_value = SimpleNamespace(
            id="pi_test_123",
            client_secret="pi_test_123_secret_value",
            status="requires_payment_method",
        )
        with (
            patch.object(stripe_payment_intents, "PAYMENT_PROVIDER", "stripe_test"),
            patch.object(stripe_payment_intents, "STRIPE_SECRET_KEY", "sk_test_" + "x" * 24),
            patch.object(stripe_payment_intents, "STRIPE_WEBHOOK_SECRET", "whsec_" + "x" * 24),
            patch.object(stripe_payment_intents.stripe, "StripeClient", return_value=client),
        ):
            result = stripe_payment_intents.create_payment_intent(
                intent_id="f59af700-65f7-4e8d-90b0-29fb21aa7001",
                amount=Decimal("4.50"),
                currency="USD",
                payment_id="5f7df6bb-7a87-4cab-bad7-03e5a813309f",
            )

        params = client.v1.payment_intents.create.call_args.args[0]
        options = client.v1.payment_intents.create.call_args.kwargs["options"]
        self.assertEqual(params["amount"], 450)
        self.assertEqual(params["currency"], "usd")
        self.assertEqual(params["automatic_payment_methods"], {"enabled": True})
        self.assertEqual(
            options,
            {"idempotency_key": "one-intent-one-payment-5f7df6bb-7a87-4cab-bad7-03e5a813309f"},
        )
        self.assertEqual(result["provider_payment_id"], "pi_test_123")
        self.assertEqual(result["status"], "requires_payment_method")

    def test_refuses_live_keys(self) -> None:
        with (
            patch.object(stripe_payment_intents, "PAYMENT_PROVIDER", "stripe_test"),
            patch.object(stripe_payment_intents, "STRIPE_SECRET_KEY", "sk_live_" + "x" * 24),
            patch.object(stripe_payment_intents, "STRIPE_WEBHOOK_SECRET", "whsec_" + "x" * 24),
            self.assertRaises(RuntimeError),
        ):
            stripe_payment_intents.validate_payment_configuration()

    def test_rejects_amounts_with_more_than_two_decimal_places(self) -> None:
        with self.assertRaises(ValueError):
            stripe_payment_intents._minor_units(Decimal("1.001"))

    def test_rejects_live_mode_webhook_events(self) -> None:
        with (
            patch.object(stripe_payment_intents, "PAYMENT_PROVIDER", "stripe_test"),
            patch.object(stripe_payment_intents, "STRIPE_SECRET_KEY", "sk_test_" + "x" * 24),
            patch.object(stripe_payment_intents, "STRIPE_WEBHOOK_SECRET", "whsec_" + "x" * 24),
            patch.object(
                stripe_payment_intents.stripe.Webhook,
                "construct_event",
                return_value={"livemode": True},
            ),
            self.assertRaises(ValueError),
        ):
            stripe_payment_intents.construct_webhook_event(b"{}", "signature")


if __name__ == "__main__":
    unittest.main()
