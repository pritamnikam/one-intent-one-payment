# Use Cases

This catalogue describes the developer and operator journeys supported by the local PoC. All payment routes assume local Docker Compose and test data. MPP/x402 are simulations; Stripe is optional and restricted to test mode.

## Actors

| Actor | Goal | Trust/permission |
|---|---|---|
| Client/developer | Submit an intent, search policy evidence, inspect own payment/audit | Uses a short-lived local JWT with only the required scopes |
| Local operator | Issue development tokens, initialize policies, inspect/recover dead letters | Controls local signing key and Compose environment |
| Outbox worker | Process durable payment requests and record outcomes | Internal service; connects to PostgreSQL and optional Stripe test mode |
| Stripe test mode | Create a test PaymentIntent and emit signed test events | External test provider; no live credentials or real payment method |

## UC-01 — Discover the service and check health

**Goal:** confirm the API is present and ready before testing.

**Main flow:** request `GET /`, `GET /healthz`, and `GET /readyz`; inspect agent cards and `/metrics`; optionally open `/docs`.

**Expected:** discovery returns static metadata; liveness confirms process availability; readiness returns HTTP 200 only while PostgreSQL and Qdrant are usable. The worker's separate metrics endpoint is on port 9100.

**Alternate flow:** if PostgreSQL or Qdrant is stopped on a disposable stack, readiness returns HTTP 503 and identifies the unavailable dependency. Liveness remains independent of those dependency checks.

## UC-02 — Search policy evidence

**Goal:** inspect policy entries relevant to a query.

**Preconditions:** Qdrant is initialized; token has `policies:read`.

**Main flow:** call `GET /policies/search?q=...` with an allowed `limit` (1–20).

**Expected:** HTTP 200 with policy ID, text, score, and contributing retrieval sources (Qdrant and/or BM25).

**Alternate flows:** missing/invalid token returns 401; missing scope returns 403; missing/empty/too-long query or out-of-range limit returns 422; unavailable retrieval dependency returns 503.

**Invariant:** search output is evidence only and cannot directly authorize a payment.

## UC-03 — Authorize a valid intent and simulate processing

**Goal:** prove one validated intent produces at most one simulated payment.

**Preconditions:** token has `intents:write`; token subject equals `intent.user_id`; intent passes schema validation and guardrails.

**Main flow:**
1. Client sends `POST /intents/authorize` with bearer token, `Idempotency-Key`, and intent.
2. API validates schema, subject and scope, searches policy evidence, and applies deterministic rules.
3. API commits payment, outbox event, and audit record in one PostgreSQL transaction.
4. Worker consumes the event and marks the payment `simulated`.
5. Owner polls `GET /payments/{payment_id}` and reads the audit trail.

**Expected:** initial payment may be `pending`; after worker processing it becomes `simulated`. No money moves.

**Business constraints:** currencies are USD/EUR; amount cannot exceed 100.00 or `max_amount`; rail must be `mpp` or `x402`.

## UC-04 — Deny an intent safely

**Goal:** return an explicit denial and preserve evidence of the decision.

**Trigger:** unsupported currency, amount above the demo ceiling, or amount above the intent's declared `max_amount`.

**Expected:** HTTP 200 with `decision.allowed=false` and `payment=null`. No payment or outbox item is created; a `payment.intent.denied` audit event is written.

## UC-05 — Retry a request or detect idempotency conflict

**Goal:** safely handle client retries without duplicate payments.

**Matching retry:** replay the exact intent with same authenticated owner, intent UUID, idempotency key, and fingerprint. API returns the same payment and audits `payment.intent.replayed`.

**Conflicting reuse:** change request fields or reuse the intent/key for a different request. API returns HTTP 409, creates no second payment, and records `payment.intent.conflict`.

## UC-06 — Read payment state with owner authorization

**Goal:** retrieve one payment's current state.

**Preconditions:** token has `payments:read`.

**Expected:** payment owner receives HTTP 200; missing token gets 401; wrong scope gets 403; another owner's or unknown payment ID receives 404; malformed UUID returns 422.

In Stripe test mode, only the authenticated owner receives the client secret. The owner confirms the PaymentIntent with Stripe.js; card data is sent directly to Stripe, not this API.

## UC-07 — Review the audit trail

**Goal:** let an authenticated principal inspect events associated with their intent/payment.

**Main flow:** call `GET /intents/{intent_id}/audit` or `GET /payments/{payment_id}/audit` with `payments:read`.

**Expected:** owner sees only allowed events. Denials may be queried by the same subject and have no payment ID. Other-owner payment history is not disclosed. Unknown identifiers return an empty array; malformed UUID returns 422.

**Audit contents:** event metadata and safe decision details. No raw webhook body, original idempotency key, client secret, merchant, or intent description.

## UC-08 — Complete a Stripe test-mode payment

**Goal:** validate the optional provider boundary without moving real money.

**Preconditions:** configure only an `sk_test_` key and Stripe CLI webhook signing secret through local secret files; configure `PAYMENT_PROVIDER=stripe_test`.

**Main flow:** submit a valid intent; worker creates a PaymentIntent with stable provider idempotency; owner reads the client secret and confirms it in Stripe.js using test mode; Stripe CLI forwards a signed webhook; API deduplicates event ID and updates status/audit.

**Expected:** statuses follow Stripe's PaymentIntent lifecycle. On `succeeded` or `canceled`, stored client secret is cleared. Duplicate webhook delivery is acknowledged but not re-applied.

## UC-09 — Diagnose and recover a failed outbox event

**Goal:** restore processing only after resolving an operational failure.

**Main flow:** worker retries transient errors up to five times with capped exponential delay; exhausted item is marked failed/dead-lettered; operator lists failed events, investigates, fixes the cause, then explicitly requeues with actor and reason.

**Expected:** requeue resets the event/payment state, appends an operator audit event, and notifies the worker. Stable provider idempotency helps avoid duplicate provider creation.

**Caution:** requeue is not cancellation or reconciliation of a provider-side PaymentIntent. Confirm provider state before recovery.

## UC-10 — Run the regression and scenario suites

**Goal:** verify expected behavior after a code or configuration change.

**Main flow:** run the automated unittest command in [TEST_STRATEGY.md](TEST_STRATEGY.md), then follow the full manual scenarios in [TESTING_GUIDE.md](TESTING_GUIDE.md).

**Expected:** automated tests cover JWT behavior, API authorization/audit routes, health/metrics, webhook validation, and mocked Stripe adapter behavior. Manual scenarios cover the actual Compose stack and optional provider workflow.

## Use case to endpoint mapping

| Use case | Main endpoints or commands |
|---|---|
| UC-01 Discovery/health | `GET /`, `/healthz`, `/readyz`, `/metrics`, `/.well-known/agent.json`, `/agent-cards`; worker `/metrics` |
| UC-02 Policy evidence | `GET /policies/search` |
| UC-03 Valid simulated payment | `POST /intents/authorize`, `GET /payments/{payment_id}` |
| UC-04 Denial | `POST /intents/authorize`, `GET /intents/{intent_id}/audit` |
| UC-05 Replay/conflict | `POST /intents/authorize`, audit routes |
| UC-06 Payment status | `GET /payments/{payment_id}` |
| UC-07 Audit history | `GET /intents/{intent_id}/audit`, `GET /payments/{payment_id}/audit` |
| UC-08 Stripe test mode | `POST /intents/authorize`, `GET /payments/{payment_id}`, `POST /webhooks/stripe` |
| UC-09 Recovery | `scripts.recover_outbox --list-failed` and `--event-id` |
| UC-10 Regression/manual testing | unittest command, [endpoint guide](TESTING_GUIDE.md) |
