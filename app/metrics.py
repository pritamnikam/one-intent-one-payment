from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "http_requests",
    "HTTP requests handled by the API.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
    registry=REGISTRY,
)
PAYMENT_DECISIONS = Counter(
    "payment_decisions",
    "Guardrail payment decisions.",
    ("outcome",),
    registry=REGISTRY,
)
OUTBOX_EVENTS = Counter(
    "payment_outbox_events",
    "Payment outbox processing outcomes.",
    ("outcome",),
    registry=REGISTRY,
)
STRIPE_WEBHOOK_EVENTS = Counter(
    "stripe_webhook_events",
    "Stripe webhook handling outcomes.",
    ("outcome",),
    registry=REGISTRY,
)

for outcome in ("allowed", "denied"):
    PAYMENT_DECISIONS.labels(outcome)

for outcome in ("processed", "retry", "dead_letter"):
    OUTBOX_EVENTS.labels(outcome)

for outcome in ("recorded", "duplicate", "ignored"):
    STRIPE_WEBHOOK_EVENTS.labels(outcome)
