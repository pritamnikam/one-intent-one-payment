# Developer Endpoint Testing Guide

This is a manual, end-to-end checklist for the local Docker Compose PoC. It covers the HTTP endpoints, authorization and validation branches, simulation flow, audit trail, operations endpoints, and optional Stripe test-mode flow.

> **Local/test use only.** The default provider is `simulation`; it never moves funds. Stripe steps must use Stripe test keys and test payment methods only. Never paste secrets into a command, source file, issue, or chat. Do not run these fault tests against production or a shared database.

## 1. Start the stack and prepare a test session

From the repository root in PowerShell:

```powershell
python -m scripts.init_local_secrets
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up --build -d
docker compose ps
```

Wait until `api` and `postgres` are healthy:

```powershell
Invoke-RestMethod http://localhost:8000/healthz
Invoke-RestMethod http://localhost:8000/readyz
```

Expected: liveness returns `status=ok`; readiness returns HTTP 200 and `postgres=ok`, `qdrant=ok`. The default provider should be `simulation`:

```powershell
Select-String -Path .env -Pattern '^PAYMENT_PROVIDER='
```

Create identities and scoped tokens. The token printed by the CLI is a credential; keep it in the current PowerShell session and do not echo it into logs:

```powershell
$owner = 'test-user-' + [guid]::NewGuid().ToString('N')
$otherOwner = 'other-user-' + [guid]::NewGuid().ToString('N')
$writeToken = (docker compose exec -T api python -m scripts.issue_token `
  --subject $owner --scope intents:write).Trim()
$readToken = (docker compose exec -T api python -m scripts.issue_token `
  --subject $owner --scope payments:read).Trim()
$policyToken = (docker compose exec -T api python -m scripts.issue_token `
  --subject $owner --scope policies:read).Trim()
$otherToken = (docker compose exec -T api python -m scripts.issue_token `
  --subject $otherOwner --scope payments:read).Trim()
```

Define a helper that makes JSON requests without printing tokens:

```powershell
$baseUrl = 'http://localhost:8000'
function Invoke-Api {
  param(
    [Parameter(Mandatory)][string]$Method,
    [Parameter(Mandatory)][string]$Path,
    [hashtable]$Headers = @{},
    $Body
  )
  $params = @{ Method = $Method; Uri = "$baseUrl$Path"; Headers = $Headers }
  if ($null -ne $Body) {
    $params.ContentType = 'application/json'
    $params.Body = $Body | ConvertTo-Json -Depth 10
  }
  Invoke-RestMethod @params
}
```

For expected error responses, use `try/catch` and inspect `$_.Exception.Response.StatusCode`; `Invoke-RestMethod` throws for non-2xx responses.

## 2. Public discovery and operations endpoints

| Test | Request | Expected |
|---|---|---|
| Root service metadata | `Invoke-Api GET /` | HTTP 200; service name, docs path, local-only safety note |
| Liveness | `Invoke-Api GET /healthz` | HTTP 200 and `status=ok`; does not test dependencies |
| Readiness | `Invoke-Api GET /readyz` | HTTP 200 with both dependencies `ok` |
| Agent card | `Invoke-Api GET /.well-known/agent.json` | HTTP 200; one capability card |
| Agent-card collection | `Invoke-Api GET /agent-cards` | HTTP 200; JSON array |
| API metrics | `Invoke-WebRequest "$baseUrl/metrics"` | HTTP 200; Prometheus text includes `http_requests_total`, `payment_decisions_total`, and `stripe_webhook_events_total` |
| Worker metrics | `Invoke-WebRequest http://localhost:9100/metrics` | HTTP 200; includes `payment_outbox_events_total` |
| OpenAPI docs | Open `http://localhost:8000/docs` in a browser | Swagger UI loads; webhook is intentionally not in the schema |

Optional dependency fault check (only on a disposable local stack):

```powershell
docker compose stop qdrant
try {
  Invoke-RestMethod http://localhost:8000/readyz
} catch {
  [int]$_.Exception.Response.StatusCode
}
docker compose start qdrant
```

While Qdrant is stopped, `/healthz` remains 200 and `/readyz` should return 503 with Qdrant unavailable. Wait for readiness to return 200 after restarting it. Do not use `docker compose down -v`; it deletes persistent data volumes.

On a disposable local stack, the PostgreSQL dependency can be checked similarly:

```powershell
docker compose stop postgres
try {
  Invoke-RestMethod http://localhost:8000/readyz
} catch {
  [int]$_.Exception.Response.StatusCode
}
docker compose start postgres
```

While PostgreSQL is stopped, `/readyz` should return 503 with PostgreSQL unavailable. Wait for the database health check and API readiness to recover.

## 3. Policy retrieval

Seed the default policies (safe to repeat):

```powershell
docker compose exec api python -m scripts.seed_policies
```

| Test | Request | Expected |
|---|---|---|
| Search as authorized reader | `Invoke-Api GET '/policies/search?q=payment+limit' @{ Authorization = "Bearer $policyToken" }` | HTTP 200; array entries have `policy_id`, `text`, `score`, and `sources` |
| Missing token | Same request without `Authorization` | HTTP 401 |
| Wrong scope | Use `$readToken` instead of `$policyToken` | HTTP 403 |
| Missing query | `Invoke-Api GET /policies/search` with policy token | HTTP 422 |
| Empty query | `Invoke-Api GET '/policies/search?q='` with policy token | HTTP 422 |
| Limit below range | `Invoke-Api GET '/policies/search?q=payment&limit=0'` with policy token | HTTP 422 |
| Limit above range | `Invoke-Api GET '/policies/search?q=payment&limit=21'` with policy token | HTTP 422 |

The valid `limit` range is 1–20; default is 5. Retrieval provides evidence only; it is not the payment authorization decision.

## 4. Authorize a valid simulated intent

Create a fresh intent ID and body:

```powershell
$intentId = [guid]::NewGuid().ToString()
$idempotencyKey = 'test-' + [guid]::NewGuid().ToString('N')
$intent = @{
  intent_id = $intentId
  user_id = $owner
  merchant = 'Example Coffee'
  description = 'One coffee'
  amount = '4.50'
  currency = 'USD'
  max_amount = '5.00'
  rail = 'x402'
}
$headers = @{
  Authorization = "Bearer $writeToken"
  'Idempotency-Key' = $idempotencyKey
}
$submitted = Invoke-Api POST /intents/authorize $headers $intent
$submitted | ConvertTo-Json -Depth 10
```

Expected:

- HTTP 200, `decision.allowed=true`, and a payment object.
- `payment.provider=simulation`, initially `pending` (the worker may change it to `simulated` before the response is inspected).
- `payment_id` is the identifier for payment-status and payment-audit requests.
- No funds are transferred.

Poll until the worker has handled the outbox event:

```powershell
$paymentId = $submitted.payment.payment_id
for ($attempt = 0; $attempt -lt 15; $attempt++) {
  $payment = Invoke-Api GET "/payments/$paymentId" @{ Authorization = "Bearer $readToken" }
  if ($payment.status -eq 'simulated') { break }
  Start-Sleep -Seconds 1
}
$payment
```

Expected terminal simulation status: `simulated`. The worker log should explicitly state that no funds moved:

```powershell
docker compose logs --no-color --tail 30 outbox-sink
```

### Rails and guardrail outcomes

Use a new UUID and a new idempotency key for each independent request. These are expected HTTP 200 responses with `decision.allowed=false` and `payment=null`; each denial is audited:

| Case | Intent fields to change | Expected decision |
|---|---|---|
| Supported alternative rail | `rail='mpp'`, amount `4.50`, currency `USD` | Allowed; simulation only |
| Unsupported currency | `currency='GBP'` | Denied; unsupported currency |
| Above demo ceiling | `amount='100.01'`, currency `USD` | Denied; ceiling is `100.00` |
| Exactly at demo ceiling | `amount='100.00'`, currency `USD`, remove `max_amount` or set it to `100.00` | Allowed |
| Above declared cap | `amount='5.01'`, `max_amount='5.00'` | Denied; exceeds intent cap |
| Exactly at declared cap | `amount='5.00'`, `max_amount='5.00'` | Allowed |

Denied intents do not create a payment or outbox event. Save the UUID used for a denied request as `$deniedIntentId`; retrieve its denial audit in section 7.

### Request validation errors

For each case, send `POST /intents/authorize` with a valid token and idempotency key, but alter one field. Expected response is HTTP 422 and no payment:

- Missing `intent_id`, `user_id`, `merchant`, `description`, or `amount`.
- Malformed UUID for `intent_id`.
- Empty/whitespace merchant or description.
- Amount equal to zero or negative, more than two decimal places, or outside the model's numeric bounds.
- Invalid currency format (for example `usd` or `US`).
- `rail` other than `mpp` or `x402`.
- `user_id` longer than 128 characters, merchant longer than 200, or description longer than 1000.
- Missing `Idempotency-Key`, empty key, or key longer than 200 characters.

`max_amount`, when provided, must be positive and within its declared decimal bounds.

### Authentication and ownership failures

| Test | Request change | Expected |
|---|---|---|
| No bearer token | Valid authorization request, remove `Authorization` | HTTP 401 |
| Invalid/expired token | Use a random string or expired token | HTTP 401 |
| Wrong scope | Use `$readToken` on `POST /intents/authorize` | HTTP 403 |
| Subject mismatch | Keep token for `$owner`, set intent `user_id` to `$otherOwner` | HTTP 403 before payment creation |

Optional expiry check: issue a one-minute token with `--expires-minutes 1`, wait at least 65 seconds, then use it on an authenticated route. Expected HTTP 401. The automated JWT tests cover expiry without waiting.

## 5. Idempotency and single-payment behavior

1. Submit the same `$intent` and `$idempotencyKey` again using the same `$headers`.
2. Expected HTTP 200 and the exact same `payment_id`; the second request is audited as `payment.intent.replayed`.
3. Change the amount or merchant in `$intent`, but reuse the same intent ID and key. Expected HTTP 409 and no second payment; a conflict audit is recorded.
4. Use the same idempotency key for a different intent UUID. Expected HTTP 409 and conflict audit.
5. Use the same intent UUID with a different idempotency key. Expected HTTP 409 and conflict audit.

The allowed replay is only a matching request (same user, intent, key, and request fingerprint). Use a new intent UUID and key after a denied or intentionally conflicting case.

## 6. Payment status endpoint

Use `$paymentId` from a successful simulation payment.

| Test | Request | Expected |
|---|---|---|
| Owner reads payment | `Invoke-Api GET "/payments/$paymentId" @{ Authorization = "Bearer $readToken" }` | HTTP 200; only the owner's payment |
| Missing token | Same request without token | HTTP 401 |
| Wrong scope | Use `$policyToken` | HTTP 403 |
| Another owner reads payment | Use `$otherToken` | HTTP 404 (does not reveal whether another owner's payment exists) |
| Unknown valid UUID | Use a newly generated UUID with `$readToken` | HTTP 404 |
| Invalid UUID path | Use `/payments/not-a-uuid` with `$readToken` | HTTP 422 |

## 7. Audit endpoints

For a successful payment:

```powershell
Invoke-Api GET "/intents/$intentId/audit" @{ Authorization = "Bearer $readToken" } |
  ConvertTo-Json -Depth 10
Invoke-Api GET "/payments/$paymentId/audit" @{ Authorization = "Bearer $readToken" } |
  ConvertTo-Json -Depth 10
```

Expected events include `payment.intent.authorized` and (after worker processing) `payment.simulated`. An idempotent retry adds `payment.intent.replayed`; a conflicting request adds `payment.intent.conflict`.

Also test:

- Read with no token: HTTP 401.
- Read using `$policyToken`: HTTP 403.
- Read another user's payment audit using `$otherToken`: HTTP 200 with an empty list; no other owner's events are disclosed.
- Query a valid but unknown intent UUID: HTTP 200 with an empty list.
- Read the denied intent UUID from the guardrail section: HTTP 200 with a `payment.intent.denied` event and `payment_id=null`.
- Invalid UUID in either audit path: HTTP 422.

Audit data intentionally excludes intent description, merchant, client secret, raw webhook body, and the original idempotency key. The key is represented by a SHA-256 digest.

## 8. Stripe test-mode payment and webhook (optional)

Skip this section if no Stripe test account and Stripe CLI are configured. Never use an `sk_live_` key or real card data.

1. Start the forwarding listener in another terminal:

   ```powershell
   stripe listen --forward-to localhost:8000/webhooks/stripe
   ```

2. Store the listener's `whsec_...` value in `.secrets/stripe_webhook_secret` and a Stripe test secret key (`sk_test_...`) in `.secrets/stripe_secret_key`. Do not print either value.
3. Set `PAYMENT_PROVIDER=stripe_test` in local `.env`, then restart the services:

   ```powershell
   docker compose up --build -d
   Invoke-RestMethod http://localhost:8000/readyz
   ```

4. Submit a fresh valid intent with `rail='x402'` and an amount accepted by the guardrails. Expected payment provider is `stripe_test`. Poll the owner-filtered payment endpoint until the status is populated and `client_secret` is present.
5. Confirm the PaymentIntent using Stripe.js and a Stripe test payment method in a browser. Do not send card details to this API.
6. Observe Stripe CLI forwarding output. A signed `payment_intent.*` event should be accepted and update payment status. Poll `GET /payments/{payment_id}` until the webhook-updated status appears. On `succeeded` or `canceled`, `client_secret` is cleared.
7. Fetch the payment audit; expected additional events include `payment.provider_intent_created` and `payment.provider_status_updated`. The audit includes event metadata, not the webhook payload or client secret.

Webhook checks:

| Test | How | Expected |
|---|---|---|
| Stripe disabled | With provider `simulation`, `POST /webhooks/stripe` and any non-empty `Stripe-Signature` header | HTTP 404 |
| Missing signature header | Stripe mode, omit `Stripe-Signature` | HTTP 422 |
| Invalid signature/body | Stripe mode, submit a non-Stripe body with a fake, non-empty signature | HTTP 400 |
| Oversized body | In Stripe mode, send a body larger than 256 KiB with any non-empty signature; the size check runs before signature verification | HTTP 413 |
| Valid non-PaymentIntent event | Use Stripe CLI to forward a legitimate signed unrelated test event | HTTP 200, `received=true`, `duplicate=false`; event is ignored |
| Valid PaymentIntent event | Complete a test PaymentIntent through Stripe CLI | HTTP 200; state and audit update |
| Duplicate delivery | Resend the same Stripe event using Stripe CLI/dashboard test tooling | HTTP 200, `duplicate=true`; no second state transition/audit event |
| Live-mode event | Not supported for local test; the adapter rejects `livemode=true` | Rejected; never use live credentials to test this |

If webhook secret is absent or invalidly configured while Stripe mode is enabled, expect HTTP 503. Restart API/worker after changing mounted secret files; they are read at process start.

To exercise the size limit without sending a real webhook:

```powershell
$largeBody = 'x' * (257 * 1024)
try {
  Invoke-WebRequest -Method Post `
    -Uri "$baseUrl/webhooks/stripe" `
    -Headers @{ 'Stripe-Signature' = 'oversize-check' } `
    -ContentType 'application/json' `
    -Body $largeBody
} catch {
  [int]$_.Exception.Response.StatusCode
}
```

Expected: HTTP 413. Do not send this request while using a public tunnel or any non-local endpoint.

## 9. Outbox retries, dead letters, and recovery

Routine happy-path coverage is provided by simulation requests. The worker retries processing failures up to five times with delays of 2, 4, 8, and 16 seconds; the configured delay is capped at five minutes. After the fifth failure, the outbox event becomes `failed` and payment becomes `failed`.

Inspect failures:

```powershell
docker compose exec outbox-sink python -m scripts.recover_outbox --list-failed
```

Only requeue an event after identifying and correcting its cause. Requeue using the UUID printed by the listing:

```powershell
docker compose exec outbox-sink python -m scripts.recover_outbox `
  --event-id <event-uuid> `
  --actor local-operator `
  --reason "Provider test configuration corrected"
```

Expected: event returns to pending, payment is reset, an audited `payment.outbox.requeued` event is written, and the worker is notified. Verify the payment and audit endpoints. Requeueing does not cancel a provider-side PaymentIntent. Do not manually edit production data to force this test.

The retry/dead-letter path and append-only database triggers are not HTTP endpoints. They were verified against a disposable PostgreSQL instance during implementation; the committed unittest suite does not currently run these database integration checks. Use only a disposable PostgreSQL instance for additional retry/dead-letter or trigger tests; do not mutate a shared database to simulate failures.

## 10. Run automated tests and cleanup

Run the auth, API, Stripe adapter, and operations tests against the Docker image:

```powershell
docker compose run --no-deps --rm `
  --volume "$($PWD.Path)\tests:/workspace/tests:ro" `
  api python -m unittest discover -s /workspace/tests -v
```

Expected: all tests pass. The local host Python interpreter may not have project dependencies installed; running through Compose uses the project's pinned dependencies.

When finished, preserve local data with:

```powershell
docker compose down
```

Avoid `docker compose down -v` unless you deliberately want to delete the local PostgreSQL and Qdrant volumes.

## Coverage map

| Surface | Covered cases |
|---|---|
| `/`, `/healthz`, `/readyz`, `/metrics` | Public discovery, liveness, healthy/unhealthy readiness, Prometheus output |
| `/.well-known/agent.json`, `/agent-cards` | Both discovery payload shapes |
| `/policies/search` | Authorized result, missing/wrong scope, query and limit validation |
| `/intents/authorize` | Allow/deny rails and thresholds, input validation, authentication, user binding, replay, conflict, persistent audit |
| `/payments/{payment_id}` | Owner, missing/wrong scope, other owner, unknown payment, malformed UUID |
| Both audit routes | Owner scoping, denied/authorized histories, missing/wrong scope, empty/unknown history, malformed UUID |
| `/webhooks/stripe` | Disabled provider, signature/config/body errors, size limit, accepted/ignored/duplicate test events, status transition |
| Outbox CLI/worker | Simulation, retries, dead letter, list/requeue, operator audit |
