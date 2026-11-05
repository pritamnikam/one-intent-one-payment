# One Intent, One Payment

A runnable, local proof of concept for expressing a single payment as an explicit intent, checking it against deterministic guardrails, and persisting one idempotent payment request. It uses FastAPI, PostgreSQL, Qdrant, BM25, reciprocal-rank fusion (RRF), and a PostgreSQL transactional outbox.

> **Simulation only:** the MPP/x402 adapters and outbox sink do not contact a wallet, merchant, bank, or payment provider. No funds are transferred. The API has no authentication and is intended for local demos only.

## Architecture

```text
Client
  └─ POST /intents/authorize
       ├─ Qdrant vector search + BM25 policy retrieval (RRF)
       ├─ Deterministic guardrail gate (amount/currency/intent cap)
       └─ PostgreSQL transaction
            ├─ one payment per intent + idempotency key
            ├─ payment_outbox row
            └─ pg_notify('payment_outbox', ...)
                 └─ LISTEN/NOTIFY sink -> simulation-only rail adapter
```

Retrieved policies are evidence for inspection, not authorization instructions. The guardrail is deterministic and does not rely on an LLM or retrieved text to approve a payment. Policy vectors use deterministic feature hashing to keep this PoC lightweight; they are not semantic model embeddings.

## Run locally with Docker Compose

Requirements: Docker Desktop (or Docker Engine) with the Compose plugin.

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The API is at <http://localhost:8000>, interactive docs at <http://localhost:8000/docs>, and Qdrant's local dashboard at <http://localhost:6333/dashboard>. The API initializes the PostgreSQL schema and seeds the Qdrant policy collection on startup. The outbox sink runs as a separate Compose service.

Stop the containers with `Ctrl+C`, or run `docker compose down`. To preserve the local database and Qdrant data, leave the named volumes in place. `docker compose down -v` deletes those volumes.

## Demo

Submit an intent. The idempotency key and intent ID ensure retries do not create a second payment:

```powershell
curl.exe -X POST http://localhost:8000/intents/authorize `
  -H "Content-Type: application/json" `
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

The first request returns a payment with `pending` status. The sink will log a simulated acceptance and change the payment status to `simulated`. Fetch it with:

```powershell
curl.exe http://localhost:8000/payments/<payment_id>
```

The authorization route accepts `rail: "mpp"` or `rail: "x402"`. Both are simulation-only. An amount above the demo ceiling of 100.00, an unsupported currency, or an amount above the intent's `max_amount` is denied without creating a payment.

Useful endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness check |
| `GET` | `/.well-known/agent.json` | Agent capability card |
| `GET` | `/agent-cards` | Agent capability cards |
| `GET` | `/policies/search?q=...` | Qdrant + BM25 hybrid retrieval |
| `POST` | `/intents/authorize` | Guard, persist, and enqueue an intent |
| `GET` | `/payments/{payment_id}` | Get the simulated payment status |

Seed or refresh the default Qdrant policies while the stack is running:

```powershell
docker compose exec api python -m scripts.seed_policies
```

## Repository layout

```text
app/
  main.py                 FastAPI endpoints and startup
  models.py               AP2Intent and PaymentReq request/response models
  guardrail/gate.py       Deterministic demo authorization rules
  cards/ramp_agent_cards.py
  rails/mpp_x402.py       Protocol-shaped, simulation-only adapter
  retrieval/hybrid_rrf.py Qdrant + BM25 retrieval with RRF
  safety/outbox.py        PostgreSQL transaction and LISTEN/NOTIFY event
  safety/sink.py          Separate listener and simulation sink
scripts/seed_policies.py  Idempotent policy seeding command
Dockerfile                Shared API/worker image
docker-compose.yml        Local PostgreSQL, Qdrant, API, and sink
```

## Scope and next steps

- The API is unauthenticated, does not identify a real user, and has no production secrets management.
- The amount/currency limits are hard-coded demo rules; policy retrieval does not change them.
- PostgreSQL enforces one payment row per intent and one row per idempotency key. A repeated matching request returns the existing payment; a conflicting reuse returns HTTP 409.
- The outbox and notification are committed atomically. PostgreSQL notifications are wake-up signals; the worker reads pending rows from the durable outbox.
- `mpp_x402.py` describes the selected protocol but intentionally does not implement external protocol handshakes or settlement.
- Before any real use, add authentication and authorization, provider-specific payment integration, key management, persistent audit controls, operational monitoring, recovery policies, and a reviewed threat model. Do not expose this unauthenticated local demo to the public internet.
