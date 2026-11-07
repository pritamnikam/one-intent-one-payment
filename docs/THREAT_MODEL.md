# Threat Model: One Intent, One Payment

## Review status and scope

This threat model was reviewed against the current local Docker Compose application, API routes, PostgreSQL outbox/audit schema, and Stripe test-mode adapter. It is an implementation review for a proof of concept, not an independent penetration test, compliance assessment, or production security approval.

The accepted deployment boundary is a developer-controlled machine running Docker Compose. Host ports bind to loopback. Only Stripe test mode is supported; MPP and x402 remain simulations. Any deployment outside that boundary requires a new review.

## Assets

| Asset | Security requirement |
|---|---|
| Local JWT signing key and issued tokens | Prevent unauthorized intent submission and payment/audit reads |
| Stripe test API and webhook secrets | Prevent unauthorized provider calls or forged test status updates |
| Intent, payment, and ownership records | Preserve integrity and restrict reads to the authenticated owner |
| Payment outbox and provider idempotency identity | Avoid lost, duplicated, or unauthorized payment processing |
| Audit records | Preserve a useful, append-only application history |
| Stripe PaymentIntent client secret | Limit disclosure to the payment owner; never expose card data to this service |
| Policy collection | Keep retrieved content from overriding deterministic authorization rules |

## Trust boundaries and data flow

1. A local client sends a bearer token and intent to FastAPI. The API validates token issuer, audience, expiry, scope, and subject-to-intent ownership.
2. The API reads policy evidence from Qdrant and evaluates deterministic amount/currency/intent guardrails. Retrieved policy text does not authorize payment.
3. PostgreSQL atomically stores a payment, outbox event, and audit event. Denials are separately recorded before a response is returned.
4. The outbox worker reads pending events and either records a simulation or creates a Stripe test-mode PaymentIntent.
5. Stripe sends signed webhooks to the API. The API verifies signatures, rejects live-mode events, deduplicates event IDs, and updates payment state.
6. Local operators can inspect failed outbox events and explicitly requeue an event; the operator and reason are audited.

The Docker host, its local users, the PostgreSQL owner, and the operator who controls mounted secret files are trusted. A compromise of any of these can bypass application-level controls.

## Abuse cases and controls

| Abuse case | Current controls | Residual risk |
|---|---|---|
| Stolen or over-scoped token submits or reads another user's payment | Short-lived HS256 tokens, fixed scope allowlist, subject binding, owner-filtered reads | No revocation list, user identity provider, or per-user issuance authorization; anyone with the local signing key can issue tokens |
| Replayed request creates duplicate payment | Unique intent/idempotency constraints, transaction-scoped advisory locks, stable Stripe idempotency key | Database compromise or provider-side behavior is outside the application guarantee |
| Intent exceeds policy limit or retrieved text attempts to influence authorization | Deterministic guardrail code; retrieval is not used as authorization logic | Demo limits are hard-coded, not a configurable production policy engine |
| Forged or replayed Stripe status event | Test-mode-only events, webhook signature verification, body-size limit, unique provider event ID, event timestamp ordering | No live Stripe validation was performed; webhook secret rotation is manual |
| Worker failure drops or repeats provider work | Durable transactional outbox, bounded retries, capped backoff, dead-letter state, operator recovery, audited recovery, stable provider idempotency key | A PaymentIntent may remain at the provider after a local failure; recovery does not cancel it and requires operator investigation |
| Audit history is changed or erased | Database trigger rejects row update/delete/truncate; application writes audit events transactionally with state changes | A database owner can alter schema, disable triggers, or modify storage; there is no external immutable archive or retention policy |
| Client secret leaks from storage or API | Owner-filtered status route, no secret in audit events, secret cleared on terminal success/cancel and dead-letter | Client secret is stored unencrypted in PostgreSQL and can be exposed by database, host, or backup compromise |
| Local secrets leak | Docker file-backed secrets, `.secrets/` ignored by Git and build context, no secret values in container environment | Files are not encrypted at rest; a host administrator or local user can read them |
| Metrics or logs reveal payment data | Metrics use route templates and bounded outcome labels; worker logs error class rather than provider exception text | Metrics are unauthenticated and rely on loopback binding; logs and local monitoring have no centralized retention or alerting |
| Denial of service or resource exhaustion | Webhook payload size cap and Qdrant client timeout | No general API rate limiting, request concurrency quota, or resource isolation |

## Operational monitoring and response

- `/healthz` is a liveness probe; `/readyz` checks PostgreSQL and Qdrant.
- API Prometheus metrics are available at `/metrics`; worker outbox counters are available on loopback port `9100`.
- Audit trails can be queried by the authenticated intent/payment owner. Operator recovery is explicit and creates a separate audit event.
- Metrics are process-local and reset on restart. The PoC does not configure alert delivery, log aggregation, audit export, backup verification, or incident-response automation.

## Required before production use

1. Replace local token issuance and shared HMAC signing with a managed identity provider and authorization policy; add key rotation/revocation.
2. Move secrets to a managed secret store and enforce least-privilege database roles. Encrypt and appropriately restrict provider secrets and client-secret data at rest.
3. Export audit events to a separately administered immutable store with retention, access review, and alerting.
4. Define provider reconciliation and cancellation procedures for failed/requeued PaymentIntents; exercise retry, dead-letter, backup, and restore paths.
5. Add rate limiting, production-grade monitoring/alerting, and production network controls.
6. Complete independent security review and live-environment provider validation before enabling any real-money flow.

Re-review this model whenever authentication, provider behavior, data retention, network exposure, deployment environment, or recovery operations change.
