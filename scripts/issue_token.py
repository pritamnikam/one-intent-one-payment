import argparse

from app.auth import (
    ALLOWED_SCOPES,
    MAX_TOKEN_LIFETIME_MINUTES,
    TOKEN_LIFETIME_MINUTES,
    issue_access_token,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Issue a short-lived, local-development API token."
    )
    parser.add_argument("--subject", required=True, help="User identity for the token.")
    parser.add_argument(
        "--scope",
        action="append",
        choices=sorted(ALLOWED_SCOPES),
        required=True,
        help="Allowed scope; repeat the flag to grant multiple scopes.",
    )
    parser.add_argument(
        "--expires-minutes",
        type=int,
        default=TOKEN_LIFETIME_MINUTES,
        choices=range(1, MAX_TOKEN_LIFETIME_MINUTES + 1),
        metavar=f"1-{MAX_TOKEN_LIFETIME_MINUTES}",
        help=f"Token lifetime in minutes (default: {TOKEN_LIFETIME_MINUTES}).",
    )
    args = parser.parse_args()
    print(issue_access_token(args.subject, set(args.scope), args.expires_minutes))


if __name__ == "__main__":
    main()
