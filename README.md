# One Intent, One Payment

A runnable, local proof of concept for expressing a single payment as an explicit intent, checking it against deterministic guardrails, and persisting one idempotent payment request. It uses FastAPI, PostgreSQL, Qdrant, BM25, reciprocal-rank fusion (RRF), and a PostgreSQL transactional outbox.

> **Local demo only:** MPP/x402 remain simulations. Optional Stripe integration is restricted to `sk_test_` keys and never uses live mode. Authentication uses locally signed development JWTs, not an external identity provider. Do not expose this setup to the public internet.

## Architecture

```text
Client
  └─ POST /intents/authorize
       ├─ Qdrant vector search + BM25 policy retrieval (RRF)
       ├─ Deterministic guardrail gate (amount/currency/intent cap)
       ├─ Scoped bearer-token authorization and subject/intent binding
       └─ PostgreSQL transaction
            ├─ one payment per intent + idempotency key
            ├─ payment_outbox row
            └─ pg_notify('payment_outbox', ...)
                 └─ LISTEN/NOTIFY sink -> simulation or Stripe test-mode adapter
```

Retrieved policies are evidence for inspection, not authorization instructions. The guardrail is deterministic and does not rely on an LLM or retrieved text to approve a payment. Policy vectors use deterministic feature hashing to keep this PoC lightweight; they are not semantic model embeddings.

## Run locally with Docker Compose

Requirements: Docker Desktop (or Docker Engine) with the Compose plugin.

```powershell
python -m scripts.init_local_secrets
Copy-Item .env.example .env
docker compose up --build
```

The API is at <http://localhost:8000>, interactive docs at <http://localhost:8000/docs>, and Qdrant's local dashboard at <http://localhost:6333/dashboard>. The API initializes the PostgreSQL schema and seeds the Qdrant policy collection on startup. The outbox sink runs as a separate Compose service. The default provider is `simulation`, so the basic demo runs without payment credentials.

The initializer creates random JWT and PostgreSQL passwords in `.secrets/` and empty Stripe key files without overwriting existing files. Compose mounts these files into only the services that need them; keys are not placed in container environment variables or the image. Keep `.secrets/` local; it is ignored by git. These local file-backed secrets are not encrypted at rest. Changing the JWT signing key invalidates previously issued tokens. If reusing the database volume from the original Compose setup, put its current password (the original default was `local-dev-password`) in `.secrets/postgres_password`; changing that file alone does not rotate a database user's password. Host ports bind only to `127.0.0.1`.

Stop the containers with `Ctrl+C`, or run `docker compose down`. To preserve the local database and Qdrant data, leave the named volumes in place. `docker compose down -v` deletes those volumes.

## Demo

Issue a short-lived local development token for the `demo-user` identity:

```powershell
$token = (docker compose exec -T api python -m scripts.issue_token `
  --subject demo-user `
  --scope intents:write `
  --scope payments:read `
  --scope policies:read).Trim()
```

Submit an intent with the token. Its subject must match `user_id`; the idempotency key and intent ID ensure retries do not create a second payment:

```powershell
curl.exe -X POST http://localhost:8000/intents/authorize `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer $token" `
  -H "Idempotency-Key: demo-coffee-001" `
  -d '{
    "intent_id": "f59af700-65f7-4e8d-90b0-29fb21aa7001",
    "user_id": "demo-user",
    "merchant": "Example Coffee",
    "description": "One coffee",
    "amount": "4.50",
    "currency": "USD",
    "max_amount": "5.00",
    "rail": "x402"
  }'
```

In simulation mode, the first request returns `pending`; the sink changes it to `simulated`. In Stripe test mode, the outbox worker creates a PaymentIntent and the authenticated owner can poll until `client_secret` is present:

```powershell
curl.exe http://localhost:8000/payments/<payment_id> -H "Authorization: Bearer $token"
```

`client_secret` is returned only from the owner-filtered payment endpoint. Use it with Stripe.js to collect and confirm a test payment method in the browser; card data is sent directly to Stripe, never through this API. For example, after creating a Card Element:

```javascript
const stripe = Stripe("pk_test_...");
const { error, paymentIntent } = await stripe.confirmCardPayment(clientSecret, {
  payment_method: { card: cardElement },
});
```

A signed Stripe webhook updates the persisted payment status. The webhook table stores event IDs and minimal metadata for deduplication, not the raw event payload.

### Enable Stripe test mode

1. In a separate PowerShell terminal, start the Stripe CLI listener while the local API is running in simulation mode:

   ```powershell
   stripe listen --forward-to localhost:8000/webhooks/stripe
   ```

   Keep it running and copy its `whsec_...` signing secret locally.
2. Set `PAYMENT_PROVIDER=stripe_test` in `.env`, write your test key (beginning with `sk_test_`) to `.secrets/stripe_secret_key`, and write the listener's `whsec_...` value to `.secrets/stripe_webhook_secret`. Never use a live `sk_live_` key.
3. Recreate the API and worker with `docker compose up --build -d`. Keep the Stripe CLI listener running; it will forward signed PaymentIntent events to the local webhook.
4. Issue a local JWT, submit an intent, poll the returned payment until it includes `client_secret`, then use Stripe.js to confirm it. Stripe's test card numbers and confirmation examples are in the [Stripe PaymentIntent guide](https://docs.stripe.com/payments/payment-intents).

The worker's Stripe create request uses a stable provider idempotency key derived from the app-generated payment ID; caller-provided idempotency keys and intent descriptions are not sent to Stripe. The API rejects live Stripe keys. PaymentIntent creation alone does not charge a card; client confirmation is required.

The authorization route accepts `rail: "mpp"` or `rail: "x402"`. Both are simulation-only. An amount above the demo ceiling of 100.00, an unsupported currency, or an amount above the intent's `max_amount` is denied without creating a payment.

### Persistent audit records

Authorization outcomes (including denials), idempotency conflicts/replays, provider processing, and Stripe status updates are recorded in PostgreSQL in the same transaction as the corresponding state change where possible. Audit records omit the intent description, merchant, credentials, client secret, and raw webhook body; the caller's idempotency key is stored only as a SHA-256 digest. The audit table rejects row updates, deletes, and truncation at the database level. These controls are defense-in-depth, not tamper-proof against a database owner, privileged operator, or database host compromise; production audit retention and export to a separately controlled immutable store are still required.

Use the authenticated owner's subject to read the audit trail:

```powershell
curl.exe http://localhost:8000/intents/<intent_id>/audit `
  -H "Authorization: ******"
```

`GET /payments/{payment_id}/audit` returns events for a payment owned by the caller. Both endpoints require `payments:read`.

Useful endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness check |
| `GET` | `/readyz` | PostgreSQL and Qdrant readiness check |
| `GET` | `/metrics` | Local Prometheus request, decision, and webhook metrics |
| `GET` | `/.well-known/agent.json` | Agent capability card |
| `GET` | `/agent-cards` | Agent capability cards |
| `GET` | `/policies/search?q=...` | Qdrant + BM25 retrieval; requires `policies:read` |
| `POST` | `/intents/authorize` | Guard, persist, and enqueue an intent; requires `intents:write` and matching token subject |
| `GET` | `/payments/{payment_id}` | Get the caller-owned payment status; requires `payments:read` |
| `GET` | `/intents/{intent_id}/audit` | Get audit events for the caller's intent; requires `payments:read` |
| `GET` | `/payments/{payment_id}/audit` | Get the caller-owned payment audit events; requires `payments:read` |
| `POST` | `/webhooks/stripe` | Stripe CLI webhook; validates Stripe's signature, no bearer token |

`/healthz` and the static discovery cards are public. Payment and policy routes require a bearer token. Tokens are issued only through the local CLI, default to 15 minutes, and may live for at most 60 minutes. A token with the wrong scope receives HTTP 403; a missing, invalid, or expired token receives HTTP 401. Payment reads are filtered by the authenticated subject.

The Compose health check uses `/readyz`; `/healthz` remains a lightweight liveness check. `/metrics` is unauthenticated for local scraping, so keep the Compose API port loopback-bound and do not expose this service publicly. The worker exposes its outbox counters on `http://127.0.0.1:9100/metrics`, also bound to loopback.

The outbox retries transient failures up to five times with exponential delays capped at five minutes. Exhausted events are marked `failed` and surfaced as dead letters. Inspect and manually requeue only after investigating the cause:

```powershell
docker compose exec outbox-sink python -m scripts.recover_outbox --list-failed
docker compose exec outbox-sink python -m scripts.recover_outbox `
  --event-id <event-uuid> `
  --actor local-operator `
  --reason "Provider test configuration corrected"
```

Requeue resets the attempt count and payment state, clears any stored client secret, records the operator and reason in the append-only audit table, and emits a PostgreSQL notification. The provider idempotency key is stable across retries. Recovery is an operator action; it does not cancel a provider-side PaymentIntent.

Run the auth, API, Stripe adapter, and operations tests with the project dependencies installed:

```powershell
python -m unittest discover -s tests -v
```

Seed or refresh the default Qdrant policies while the stack is running:

```powershell
docker compose exec api python -m scripts.seed_policies
```

## Repository layout

```text
app/
  auth.py                 Local JWT validation and scope authorization
  main.py                 FastAPI endpoints and startup
  models.py               AP2Intent and PaymentReq request/response models
  guardrail/gate.py       Deterministic demo authorization rules
  cards/ramp_agent_cards.py
  rails/mpp_x402.py       Protocol-shaped, simulation-only adapter
  rails/stripe_payment_intents.py Stripe test-mode PaymentIntent client
  retrieval/hybrid_rrf.py Qdrant + BM25 retrieval with RRF
  safety/outbox.py        PostgreSQL transaction and LISTEN/NOTIFY event
  safety/sink.py          Retrying listener, dead-lettering, and simulation sink
  metrics.py              Prometheus API and worker metrics
scripts/issue_token.py    Short-lived local development token issuer
scripts/recover_outbox.py Inspect and requeue failed outbox events
scripts/seed_policies.py  Idempotent policy seeding command
tests/                    Authentication and API authorization tests
THREAT_MODEL.md            Reviewed local PoC threat model and production gaps
Dockerfile                Shared API/worker image
docker-compose.yml        Local PostgreSQL, Qdrant, API, and sink
```

## Authentication limitations and production readiness

- The local token issuer is an operator utility, not a user sign-in flow or identity provider. Anyone with `JWT_SECRET` can issue tokens with any supported scope; rotate the key to invalidate all tokens.
- The local JWT setup does not include refresh tokens, revocation lists, an external identity provider, or production key management. Use a managed identity provider and secret store before deployment.
- The Stripe PaymentIntent client secret is stored in PostgreSQL for owner-only retrieval and is not encrypted in this local PoC. Never use real card data or live Stripe credentials.
- Docker Compose file-backed secrets reduce exposure through container environment inspection but are not an encrypted vault. Protect the host and `.secrets/` directory; production deployments need a managed secret store.
- Provider failures are retried up to five times with capped backoff, then dead-lettered for operator review and explicit recovery.
- Existing payments created before this migration have no owner recorded and are not visible through the owner-filtered payment endpoint.
- The amount/currency limits are hard-coded demo rules; policy retrieval does not change them.
- PostgreSQL enforces one payment row per intent and one row per idempotency key. A repeated matching request returns the existing payment; a conflicting reuse returns HTTP 409.
- The outbox and notification are committed atomically. PostgreSQL notifications are wake-up signals; the worker reads pending rows from the durable outbox.
- Audit events are append-only for the application database role, but the database owner can still alter the schema or disable the guard trigger.
- `mpp_x402.py` describes MPP/x402 protocols; Stripe is a separate test-mode provider, not an implementation of either protocol.
- The threat model is reviewed against this local implementation but is not an independent security assessment. See [THREAT_MODEL.md](./THREAT_MODEL.md) for residual risks and production requirements.
- Do not use the development JWT setup for live payments or expose this service to the public internet.
