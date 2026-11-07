# Test Suite Strategy

This document distinguishes what is automated today from manual or infrastructure-dependent validation. The name of a test layer describes what it validates, not a claim that every layer is fully automated.

## Current coverage by test level

| Level | Current status | What it covers | How to run |
|---|---|---|---|
| Unit | Automated | JWT issuance/verification, scope rules, secret reading, Stripe adapter parameters and validation | `python -m unittest discover -s tests -p test_auth.py -v` and `python -m unittest discover -s tests -p test_stripe_payment_intents.py -v` |
| API/functional component | Automated with FastAPI TestClient and mocks | Route-level auth, subject binding, audit route scoping, denied/conflicting requests, readiness, metrics, webhook signature/error flow | Included in `python -m unittest discover -s tests -v` |
| Integration | Database portion exercised manually against disposable PostgreSQL; not committed as an automated suite | PostgreSQL schema initialization/idempotence, audit append-only triggers, event dedupe, outbox retry/dead-letter/requeue transaction behavior | Follow the disposable DB integration notes in the [endpoint guide](TESTING_GUIDE.md); do not use shared or production databases |
| Functional / end-to-end | Manual scenarios documented; optional Stripe flow requires Stripe test account and CLI | Compose services together, Qdrant retrieval, real PostgreSQL persistence, worker processing, signed webhook delivery, user/operator workflows | Follow [TESTING_GUIDE.md](TESTING_GUIDE.md) |
| Regression | Automated suite is the current regression gate; no separate regression runner | Re-run all 27 committed tests after code changes; add a focused test for every bug fix | Run the full unittest command below |

### Important boundary

`tests/test_api_auth.py` exercises the FastAPI app with mocked persistence, policy-search, webhook, and health dependencies. It is not a full live-stack integration test. The Stripe unit tests mock the Stripe SDK and do not call Stripe. Manual end-to-end Stripe validation is test-mode-only.

At the time this guide was written, the repository contains 27 unittest cases:

- `test_auth.py`: token claims, accepted/unknown scopes, bearer validation, expiry, secret strength and file precedence.
- `test_api_auth.py`: route auth/scope/subject binding, denial/conflict audits, owner-only payment/audit reads, readiness, metrics, webhook event handling and invalid signatures.
- `test_stripe_payment_intents.py`: stable provider idempotency, live-key rejection, decimal precision, and live-mode webhook rejection.

The PostgreSQL retry/dead-letter/recovery and append-only trigger integration checks were performed against a disposable PostgreSQL container during implementation. They are not yet checked into `tests/`; do not imply they run as part of unittest discovery.

## Run automated tests

Use the Compose image so the pinned dependencies are available:

```powershell
docker compose run --no-deps --rm `
  --volume "$($PWD.Path)\tests:/workspace/tests:ro" `
  api python -m unittest discover -s /workspace/tests -v
```

Expected: all tests pass. The API container is started for the test command but test fixtures mock external service access; no real payment or webhook is triggered.

For a local interpreter with dependencies installed:

```powershell
python -m unittest discover -s tests -v
```

## Regression policy

For every code change:

1. Run the entire committed unittest suite, not only the new test.
2. Run `git diff --check`.
3. If endpoint contracts, Compose wiring, dependencies, or docs commands changed, validate the corresponding artifact:
   - Compose: `docker compose config --quiet`
   - Python syntax/imports: compile/import with the project Docker image
   - Routes: TestClient tests and, when feasible, local Compose smoke checks
   - Database schema or transaction behavior: disposable PostgreSQL integration test
4. For provider changes, keep SDK tests mocked by default; only run manual live-provider **test mode** validation with locally stored test credentials.
5. Preserve deterministic, isolated tests. Never require real secrets, a public network target, live payment credentials, or destructive changes to a shared database.

## Functional test layers

### API functional components

Automated tests assert response codes and key request/result behavior for authentication failures, scope failures, subject mismatch, allowed/denied requests, idempotency conflict handling, owner-scoped reads, readiness, metrics, and webhook validation. They mock database and external-service boundaries.

### Local end-to-end

The [Endpoint Testing Guide](TESTING_GUIDE.md) includes complete PowerShell journeys for:

- service and dependency health;
- valid policy retrieval and validation/authentication negatives;
- successful simulation and terminal status polling;
- guardrail denial cases and request schema errors;
- same-request replay and conflicting key/intent reuse;
- payment and audit ownership;
- optional Stripe test PaymentIntent creation/confirmation/webhook;
- worker metrics, dead-letter inspection and audited recovery.

Use only a developer-controlled local stack. The guide's dependency-stop/fault checks interrupt services; do not use them against shared environments.

### Database integration tests

To improve integration-test automation, add tests using an isolated PostgreSQL service/container and a unique database per test run. Assert database schema upgrades are repeatable, operations are transactional, and audit update/delete/truncate attempts are rejected. Keep Qdrant/provider interactions mocked unless the test explicitly provisions disposable instances.

## Endpoint coverage summary

| Endpoint | Automated component coverage | Manual end-to-end scenarios |
|---|---|---|
| `GET /` | Not currently asserted directly | Discovery |
| `GET /healthz` | Not currently asserted directly | Liveness |
| `GET /readyz` | Healthy response with mocked dependencies | Real dependency healthy/unavailable |
| `GET /metrics` | Presence of request metric | Prometheus output smoke check |
| `GET /.well-known/agent.json` | Not currently asserted directly | Agent card shape |
| `GET /agent-cards` | Not currently asserted directly | Card list |
| `GET /policies/search` | Missing/wrong-scope authorization | Qdrant/BM25 response, input bounds, dependency errors |
| `POST /intents/authorize` | Missing token, wrong scope, subject mismatch, allowed request, denied/conflict audit | Persistence, worker transition, guardrail thresholds, validation and idempotency |
| `GET /payments/{payment_id}` | Owner-filtered data access | Owner, other owner, unknown/malformed IDs, provider status |
| `GET /intents/{intent_id}/audit` | Owner/subject-scoped route behavior | Persisted success and denial events |
| `GET /payments/{payment_id}/audit` | Owner-scoped route behavior | Persisted event trail and other-owner isolation |
| `POST /webhooks/stripe` | Valid mocked event handling and invalid signature | Stripe CLI signed test events, duplicate delivery, body-size/config errors |

The agent discovery endpoints, direct `/healthz`, and some validation branches are presently manual-only. Add automated tests as those contracts become critical.
