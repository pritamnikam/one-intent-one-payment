import secrets
from pathlib import Path

SECRETS = Path(".secrets")
DEFAULTS = {
    "jwt_secret": secrets.token_urlsafe(48),
    "postgres_password": secrets.token_urlsafe(32),
    "stripe_secret_key": "",
    "stripe_webhook_secret": "",
}


def main() -> None:
    SECRETS.mkdir(exist_ok=True)
    created: list[str] = []
    for name, value in DEFAULTS.items():
        path = SECRETS / name
        try:
            with path.open("x", encoding="utf-8", newline="\n") as secret_file:
                secret_file.write(value)
            created.append(name)
        except FileExistsError:
            continue
    if created:
        print(f"Created local secret files: {', '.join(created)}")
    else:
        print("Local secret files already exist; left them unchanged.")
    print("These files are ignored by git. Do not commit or share them.")


if __name__ == "__main__":
    main()
