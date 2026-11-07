import argparse
import asyncio
from uuid import UUID

from app.safety.outbox import list_failed_outbox_events, recover_outbox_event


async def _run(args: argparse.Namespace) -> None:
    if args.list_failed:
        events = await list_failed_outbox_events()
        if not events:
            print("No failed outbox events.")
            return
        for event in events:
            print(
                f"event_id={event['event_id']} payment_id={event['payment_id']} "
                f"attempts={event['attempts']} last_error={event['last_error'] or '-'} "
                f"created_at={event['created_at'].isoformat()}"
            )
        return

    if not args.actor or not args.reason:
        raise SystemExit("--actor and --reason are required when requeueing an event.")
    payment_id = await recover_outbox_event(args.event_id, args.actor, args.reason)
    print(f"Requeued event_id={args.event_id} payment_id={payment_id}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or recover failed payment outbox events.")
    recovery = parser.add_mutually_exclusive_group(required=True)
    recovery.add_argument("--list-failed", action="store_true")
    recovery.add_argument("--event-id", type=UUID)
    parser.add_argument("--actor", help="Operator identity recorded in the audit trail.")
    parser.add_argument("--reason", help="Short operational reason; do not include sensitive data.")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
