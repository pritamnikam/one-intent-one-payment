import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

import app.main as main
from app.auth import issue_access_token
from app.models import PaymentReq


class FakePolicySearch:
    def search(self, query: str, limit: int) -> list[dict]:
        return [
            {
                "policy_id": "amount-limit",
                "text": "Demo amount limit.",
                "score": 0.02,
                "sources": ["qdrant", "bm25"],
            }
        ]


class ApiAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret_patch = patch.dict(os.environ, {"JWT_SECRET": "test-key-" + "x" * 40})
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        main.app.state.policy_search = FakePolicySearch()
        self.client = TestClient(main.app)

    @staticmethod
    def _intent(user_id: str = "demo-user") -> dict:
        return {
            "intent_id": "f59af700-65f7-4e8d-90b0-29fb21aa7001",
            "user_id": user_id,
            "merchant": "Example Coffee",
            "description": "One coffee",
            "amount": "4.50",
            "currency": "USD",
            "rail": "x402",
        }

    @staticmethod
    def _token(subject: str, *scopes: str) -> str:
        return issue_access_token(subject, set(scopes))

    def test_intent_endpoint_requires_bearer_token(self) -> None:
        response = self.client.post(
            "/intents/authorize",
            headers={"Idempotency-Key": "test-key"},
            json=self._intent(),
        )
        self.assertEqual(response.status_code, 401)

    def test_intent_endpoint_requires_write_scope(self) -> None:
        token = self._token("demo-user", "payments:read")
        response = self.client.post(
            "/intents/authorize",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "test-key",
            },
            json=self._intent(),
        )
        self.assertEqual(response.status_code, 403)

    def test_policy_search_requires_read_scope(self) -> None:
        token = self._token("demo-user", "payments:read")
        response = self.client.get(
            "/policies/search",
            params={"q": "payment limit"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_intent_subject_must_match_authenticated_user(self) -> None:
        token = self._token("another-user", "intents:write")
        response = self.client.post(
            "/intents/authorize",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "test-key",
            },
            json=self._intent(),
        )
        self.assertEqual(response.status_code, 403)

    def test_authenticated_subject_can_submit_own_intent(self) -> None:
        token = self._token("demo-user", "intents:write")
        payment = PaymentReq(
            intent_id=UUID(self._intent()["intent_id"]),
            amount="4.50",
            currency="USD",
            merchant="Example Coffee",
            rail="x402",
            idempotency_key="test-key",
        )
        with patch(
            "app.main.submit_payment",
            new_callable=AsyncMock,
            return_value=(payment, True),
        ) as submit_payment:
            response = self.client.post(
                "/intents/authorize",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "test-key",
                },
                json=self._intent(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["decision"]["allowed"])
        self.assertEqual(response.json()["decision"]["policy_ids"], ["amount-limit"])
        submit_payment.assert_awaited_once()

    def test_payment_status_queries_only_authenticated_owner(self) -> None:
        token = self._token("demo-user", "payments:read")
        payment_id = UUID("5f7df6bb-7a87-4cab-bad7-03e5a813309f")
        payment = PaymentReq(
            payment_id=payment_id,
            intent_id=UUID(self._intent()["intent_id"]),
            amount="4.50",
            currency="USD",
            merchant="Example Coffee",
            rail="x402",
            idempotency_key="test-key",
        )
        with patch("app.main.get_payment", new_callable=AsyncMock, return_value=payment) as get_payment:
            response = self.client.get(
                f"/payments/{payment_id}",
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 200)
        get_payment.assert_awaited_once_with(payment_id, "demo-user", main.DATABASE_URL)

    def test_denied_intent_is_persistently_audited(self) -> None:
        token = self._token("demo-user", "intents:write")
        intent = self._intent()
        intent["amount"] = "101.00"
        with patch("app.main.record_denied_intent", new_callable=AsyncMock) as record_denied:
            response = self.client.post(
                "/intents/authorize",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "denied-test-key",
                },
                json=intent,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["decision"]["allowed"])
        record_denied.assert_awaited_once()
        self.assertEqual(record_denied.await_args.args[1].allowed, False)

    def test_conflicting_intent_is_audited_before_returning_conflict(self) -> None:
        token = self._token("demo-user", "intents:write")
        with (
            patch("app.main.submit_payment", new_callable=AsyncMock, side_effect=ValueError("conflict")),
            patch("app.main.record_conflicting_intent", new_callable=AsyncMock) as record_conflict,
        ):
            response = self.client.post(
                "/intents/authorize",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": "conflict-test-key",
                },
                json=self._intent(),
            )

        self.assertEqual(response.status_code, 409)
        record_conflict.assert_awaited_once()

    def test_intent_audit_is_filtered_to_authenticated_subject(self) -> None:
        token = self._token("demo-user", "payments:read")
        intent_id = UUID(self._intent()["intent_id"])
        with patch("app.main.get_intent_audit", new_callable=AsyncMock, return_value=[]) as get_audit:
            response = self.client.get(
                f"/intents/{intent_id}/audit",
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 200)
        get_audit.assert_awaited_once_with(intent_id, "demo-user", main.DATABASE_URL)

    def test_payment_audit_is_filtered_to_authenticated_owner(self) -> None:
        token = self._token("demo-user", "payments:read")
        payment_id = UUID("5f7df6bb-7a87-4cab-bad7-03e5a813309f")
        with patch("app.main.get_payment_audit", new_callable=AsyncMock, return_value=[]) as get_audit:
            response = self.client.get(
                f"/payments/{payment_id}/audit",
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 200)
        get_audit.assert_awaited_once_with(payment_id, "demo-user", main.DATABASE_URL)

    def test_readiness_checks_postgres_and_qdrant(self) -> None:
        with (
            patch("app.main._check_postgres", new_callable=AsyncMock),
            patch("app.main._check_qdrant", new_callable=AsyncMock),
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")

    def test_metrics_endpoint_exposes_request_metrics(self) -> None:
        response = self.client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertIn("http_requests_total", response.text)

    def test_stripe_webhook_verifies_and_records_payment_event(self) -> None:
        event = {
            "id": "evt_test_123",
            "type": "payment_intent.succeeded",
            "created": 1_767_623_400,
            "livemode": False,
            "data": {"object": {"id": "pi_test_123", "status": "succeeded"}},
        }
        with (
            patch("app.main.PAYMENT_PROVIDER", "stripe_test"),
            patch("app.main.construct_webhook_event", return_value=event) as construct_event,
            patch("app.main.record_stripe_webhook_event", new_callable=AsyncMock, return_value=True) as record_event,
        ):
            response = self.client.post(
                "/webhooks/stripe",
                content=b'{"id":"evt_test_123"}',
                headers={"Stripe-Signature": "test-signature"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"received": True, "duplicate": False})
        construct_event.assert_called_once_with(b'{"id":"evt_test_123"}', "test-signature")
        record_event.assert_awaited_once()

    def test_stripe_webhook_rejects_invalid_signature(self) -> None:
        with (
            patch("app.main.PAYMENT_PROVIDER", "stripe_test"),
            patch("app.main.construct_webhook_event", side_effect=ValueError("bad signature")),
        ):
            response = self.client.post(
                "/webhooks/stripe",
                content=b"invalid",
                headers={"Stripe-Signature": "invalid"},
            )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
