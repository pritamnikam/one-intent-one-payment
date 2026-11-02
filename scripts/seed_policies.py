import argparse
import os

from app.retrieval.hybrid_rrf import HybridPolicySearch, create_qdrant_client


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the demo policy collection in Qdrant.")
    parser.add_argument(
        "--qdrant-url",
        default=os.getenv("QDRANT_URL", "http://localhost:6333"),
        help="Qdrant HTTP URL (default: QDRANT_URL or http://localhost:6333)",
    )
    args = parser.parse_args()

    client = create_qdrant_client(args.qdrant_url)
    try:
        search = HybridPolicySearch(client)
        search.ensure_ready()
        search.upsert_policies()
    finally:
        client.close()
    print("Seeded payment policy documents in Qdrant.")


if __name__ == "__main__":
    main()
