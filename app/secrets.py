import os
from pathlib import Path


def read_secret(name: str) -> str:
    secret_file = os.getenv(f"{name}_FILE")
    if secret_file:
        try:
            return Path(secret_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"Could not read configured secret file for {name}.") from error
    return os.getenv(name, "").strip()
