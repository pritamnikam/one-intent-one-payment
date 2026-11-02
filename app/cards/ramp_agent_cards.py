AGENT_CARDS = [
    {
        "id": "one-intent-one-payment",
        "name": "One Intent, One Payment",
        "description": "A local proof of concept for guarded, idempotent payment intents.",
        "url": "/",
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "skills": [
            {
                "id": "authorize-intent",
                "name": "Authorize a payment intent",
                "description": "Apply deterministic limits and enqueue a simulated payment.",
            },
            {
                "id": "search-policies",
                "name": "Search payment policies",
                "description": "Retrieve policy evidence using Qdrant and BM25 reciprocal-rank fusion.",
            },
        ],
        "authentication": {"schemes": []},
        "metadata": {"execution": "local-simulation-only"},
    }
]
