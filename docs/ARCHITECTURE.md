# Architecture

This document describes the local proof of concept as C4-style context, container, and component views, followed by its primary runtime sequence. The diagrams use Mermaid flowcharts and sequence diagrams so they render in GitHub and common Markdown viewers.

> These diagrams represent the current implementation. Stripe is optional and test-mode-only; the MPP/x402 adapters do not contact providers or move funds.

## C1 — System context

```mermaid
flowchart LR
    developer["Person: Developer / local client<br/>Submits intents, reads own payments"]
    operator["Person: Local operator<br/>Issues dev tokens, inspects/requeues dead letters"]
    paymentSystem["System: One Intent, One Payment<br/>FastAPI intent authorization PoC"]
    stripe["External system: Stripe test mode<br/>Creates test PaymentIntents; sends signed events"]

    developer -->|"HTTPS-style local HTTP: JWT, intent, status/audit requests"| paymentSystem
    operator -->|"Local CLI: token issuance and recovery"| paymentSystem
    paymentSystem -. "Optional test PaymentIntent creation" .-> stripe
    stripe -. "Stripe CLI forwards signed test webhook" .-> paymentSystem
```

**Trust boundary:** the local machine and its Docker services are trusted. Host services are loopback-bound. The local JWT issuer is an operator utility, not a production identity provider. Stripe is not used in the default simulation flow.

## C2 — Containers

```mermaid
flowchart TB
    subgraph host["Developer workstation — Docker Compose / loopback ports"]
        client["Developer client<br/>PowerShell / curl / browser"]
        api["Container: FastAPI API<br/>JWT + scopes, validation, policy lookup,<br/>guardrails, audit/status/webhook routes"]
        worker["Container: Outbox worker<br/>LISTEN/NOTIFY + durable poll,<br/>retry/dead-letter, provider dispatch"]
        postgres[("Container: PostgreSQL<br/>payments, outbox, audit,<br/>Stripe webhook dedupe")]
        qdrant[("Container: Qdrant<br/>policy vectors")]
    end
    ranker["In-process API component:<br/>BM25 + reciprocal-rank fusion"]
    bm25["In-process BM25 policy corpus"]
    stripe["External: Stripe test mode"]

    client -->|"HTTP :8000"| api
    api -->|"SQL / psycopg"| postgres
    api -->|"Vector query"| qdrant
    api --> ranker
    ranker -->|"Keyword ranking"| bm25
    ranker -->|"Vector ranking"| qdrant
    worker -->|"SQL + LISTEN/NOTIFY"| postgres
    worker -->|"Optional create PaymentIntent"| stripe
    stripe -->|"Signed event via Stripe CLI"| api
    api -. "API metrics :8000/metrics" .-> client
    worker -. "Worker metrics :9100/metrics" .-> client
```

The in-process policy search combines Qdrant's ranked results with local BM25 rankings using reciprocal-rank fusion. This hybrid search retrieves policy evidence; the guardrail decision is made by deterministic Python code.

## C3 — API and worker components

```mermaid
flowchart LR
    subgraph api["FastAPI API container"]
        routes["HTTP routes<br/>discovery, health/readiness,<br/>policy, intent, payment, audit, webhook"]
        auth["JWT validation + scope dependencies"]
        models["Pydantic request/response validation"]
        search["HybridPolicySearch<br/>Qdrant + BM25/RRF"]
        guard["Deterministic guardrail gate<br/>currency, amount, intent cap"]
        repository["PostgreSQL safety/outbox functions<br/>idempotency, audit, payment reads"]
        webhook["Stripe webhook adapter<br/>signature, test-mode, event ordering/dedupe"]

        routes --> auth
        routes --> models
        routes --> search
        routes --> guard
        routes --> repository
        routes --> webhook
    end

    subgraph worker["Outbox worker container"]
        listener["PostgreSQL listener + pending-event poll"]
        dispatcher["Provider dispatcher"]
        retries["Retry / backoff / dead-letter"]
        recovery["Operator recovery CLI"]
        listener --> dispatcher
        dispatcher --> retries
        recovery --> listener
    end

    postgres[("PostgreSQL")]
    qdrant[("Qdrant")]
    stripe["Stripe test mode"]
    search --> qdrant
    repository --> postgres
    listener --> postgres
    webhook --> postgres
    dispatcher --> postgres
    dispatcher -. optional .-> stripe
```

## Runtime sequence — authorization to status

```mermaid
sequenceDiagram
    autonumber
    participant Client
    participant API
    participant Auth
    participant Search
    participant Gate
    participant DB
    participant Worker
    participant Provider

    Client->>API: Submit intent with bearer token and idempotency key
    API->>Auth: Validate token and required scope
    Auth-->>API: Return authenticated principal
    API->>API: Check intent owner matches principal
    API->>Search: Retrieve policy evidence
    Search-->>API: Return ranked policy IDs
    API->>Gate: Check currency and amount limits
    alt Denied
        Gate-->>API: Return denied decision
        API->>DB: Append denial audit event
        API-->>Client: Return decision with no payment
    else Allowed
        Gate-->>API: Return allowed decision
        API->>DB: Save payment, outbox event, and audit
        DB-->>API: Commit and return payment ID
        API-->>Client: Return pending payment
        Worker->>DB: Read pending outbox event
        Worker->>Provider: Process event
        Provider-->>Worker: Return simulation or test payment result
        Worker->>DB: Save payment status and audit event
        opt Stripe test mode
            Provider-->>API: Send signed test webhook
            API->>DB: Deduplicate event and update payment
        end
        Client->>API: Request payment status and audit
        API->>DB: Read records for authenticated owner
        DB-->>API: Return payment and audit events
        API-->>Client: Return current status and events
    end
```

## Data and state boundaries

- **Payment write:** the API persists the payment row and outbox row atomically; PostgreSQL uniqueness and advisory locks enforce one matching payment per intent and idempotency key.
- **Asynchronous dispatch:** `pg_notify` is a wake-up signal. The outbox remains the durable source of work; the worker polls pending events even when a notification is missed.
- **Audit:** authorization outcomes and important payment transitions are appended to PostgreSQL. Database triggers reject audit row updates, deletes, and truncation, but a database owner can bypass these protections.
- **Payment ownership:** status and audit reads filter by the authenticated subject. Unknown and another-owner payment IDs both appear as not found from the status endpoint.
- **Stripe test mode:** only test keys and signed test events are accepted. The owner confirms the PaymentIntent client-side; card details must never pass through this API.
- **Policy search:** the Qdrant vectors use deterministic feature hashing, not semantic model embeddings. Retrieved text does not act as an authorization rule.

## HTTP route map

| Route | Responsibility | Authentication |
|---|---|---|
| `GET /` | Service metadata | Public |
| `GET /healthz` | Liveness | Public |
| `GET /readyz` | PostgreSQL and Qdrant readiness | Public |
| `GET /metrics` | API Prometheus metrics | Public; loopback-bound |
| `GET /.well-known/agent.json` | Agent capability card | Public |
| `GET /agent-cards` | Agent capability cards | Public |
| `GET /policies/search` | Hybrid policy evidence search | `policies:read` |
| `POST /intents/authorize` | Validate, decide, audit, persist, enqueue | `intents:write` and matching subject |
| `GET /payments/{payment_id}` | Payment status | `payments:read`, owner-filtered |
| `GET /intents/{intent_id}/audit` | Intent audit events | `payments:read`, subject-filtered |
| `GET /payments/{payment_id}/audit` | Payment audit events | `payments:read`, owner-filtered |
| `POST /webhooks/stripe` | Stripe test webhook | Stripe signature; hidden from OpenAPI |

Worker metrics are exposed separately at `http://127.0.0.1:9100/metrics`. Operator outbox inspection and requeue are local CLI operations rather than HTTP routes.
