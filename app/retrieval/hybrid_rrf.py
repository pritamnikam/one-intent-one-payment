import hashlib
import re
from collections import defaultdict
from typing import Any

from qdrant_client import QdrantClient, models
from rank_bm25 import BM25Okapi

COLLECTION_NAME = "payment-policies"
VECTOR_SIZE = 384
RRF_CONSTANT = 60

POLICIES = [
    {
        "policy_id": "amount-limit",
        "text": "Demo payments are limited to 100.00 units of the requested currency. "
        "Amounts above this ceiling must be denied.",
    },
    {
        "policy_id": "currency-support",
        "text": "Only USD and EUR are supported by the local payment simulation. "
        "Other currencies must be denied.",
    },
    {
        "policy_id": "intent-bound",
        "text": "A payment amount must not exceed the maximum amount explicitly authorized "
        "by its intent. One intent may create at most one payment.",
    },
    {
        "policy_id": "simulation-only",
        "text": "The MPP and x402 adapters in this proof of concept are protocol-shaped "
        "simulations. They never move funds or contact a payment provider.",
    },
]


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _hashed_vector(text: str) -> list[float]:
    vector = [0.0] * VECTOR_SIZE
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % VECTOR_SIZE
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    magnitude = sum(value * value for value in vector) ** 0.5
    if magnitude:
        vector = [value / magnitude for value in vector]
    return vector


class HybridPolicySearch:
    def __init__(self, client: QdrantClient) -> None:
        self.client = client
        self._bm25 = BM25Okapi([_tokens(policy["text"]) for policy in POLICIES])

    def ensure_ready(self) -> None:
        if not self.client.collection_exists(COLLECTION_NAME):
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=models.VectorParams(size=VECTOR_SIZE, distance=models.Distance.COSINE),
            )

        existing = self.client.count(collection_name=COLLECTION_NAME, exact=True).count
        if existing == 0:
            self.upsert_policies()

    def upsert_policies(self) -> None:
        self.client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                models.PointStruct(
                    id=index + 1,
                    vector=_hashed_vector(policy["text"]),
                    payload=policy,
                )
                for index, policy in enumerate(POLICIES)
            ],
            wait=True,
        )

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        dense_points = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=_hashed_vector(query),
            limit=max(limit * 2, limit),
            with_payload=True,
        ).points

        dense_ids = [
            point.payload["policy_id"]
            for point in dense_points
            if point.payload and "policy_id" in point.payload
        ]
        bm25_scores = self._bm25.get_scores(_tokens(query))
        bm25_indexes = sorted(
            range(len(POLICIES)),
            key=lambda index: bm25_scores[index],
            reverse=True,
        )
        bm25_ids = [POLICIES[index]["policy_id"] for index in bm25_indexes]

        scores: dict[str, float] = defaultdict(float)
        sources: dict[str, list[str]] = defaultdict(list)
        for source, ranked_ids in (("qdrant", dense_ids), ("bm25", bm25_ids)):
            for rank, policy_id in enumerate(ranked_ids, start=1):
                scores[policy_id] += 1.0 / (RRF_CONSTANT + rank)
                sources[policy_id].append(source)

        policies_by_id = {policy["policy_id"]: policy for policy in POLICIES}
        ranked = sorted(scores, key=scores.get, reverse=True)[:limit]
        return [
            {
                **policies_by_id[policy_id],
                "score": scores[policy_id],
                "sources": sources[policy_id],
            }
            for policy_id in ranked
        ]


def create_qdrant_client(url: str) -> QdrantClient:
    return QdrantClient(url=url, timeout=10)
