import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError

from app.secrets import read_secret

ALLOWED_SCOPES = frozenset({"intents:write", "payments:read", "policies:read"})
JWT_ISSUER = os.getenv("JWT_ISSUER", "one-intent-one-payment-local")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "one-intent-one-payment-api")
TOKEN_LIFETIME_MINUTES = 15
MAX_TOKEN_LIFETIME_MINUTES = 60

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]


def _jwt_secret() -> str:
    secret = read_secret("JWT_SECRET")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET must be set to at least 32 bytes.")
    return secret


def validate_jwt_configuration() -> None:
    _jwt_secret()


def issue_access_token(
    subject: str,
    scopes: set[str],
    lifetime_minutes: int = TOKEN_LIFETIME_MINUTES,
) -> str:
    if not subject or len(subject) > 128:
        raise ValueError("Subject must contain between 1 and 128 characters.")
    if not scopes or not scopes.issubset(ALLOWED_SCOPES):
        raise ValueError("Provide one or more valid authorization scopes.")
    if not 1 <= lifetime_minutes <= MAX_TOKEN_LIFETIME_MINUTES:
        raise ValueError(f"Token lifetime must be between 1 and {MAX_TOKEN_LIFETIME_MINUTES} minutes.")

    now = datetime.now(timezone.utc)
    claims = {
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
        "sub": subject,
        "scope": " ".join(sorted(scopes)),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=lifetime_minutes),
        "jti": str(uuid4()),
    }
    return jwt.encode(claims, _jwt_secret(), algorithm="HS256")


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = jwt.decode(
            credentials.credentials,
            _jwt_secret(),
            algorithms=["HS256"],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={"require": ["iss", "aud", "sub", "scope", "iat", "nbf", "exp", "jti"]},
        )
        subject = claims["sub"]
        scope_claim = claims["scope"]
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 128
            or not isinstance(scope_claim, str)
        ):
            raise InvalidTokenError("Invalid subject or scopes.")
        scopes = frozenset(scope_claim.split())
        if not scopes or not scopes.issubset(ALLOWED_SCOPES):
            raise InvalidTokenError("Invalid subject or scopes.")
    except (InvalidTokenError, KeyError, TypeError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    return Principal(subject=subject, scopes=scopes)


def require_scopes(*required_scopes: str):
    if not required_scopes or not set(required_scopes).issubset(ALLOWED_SCOPES):
        raise ValueError("Dependencies must request valid authorization scopes.")

    async def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not set(required_scopes).issubset(principal.scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token does not grant the required scope.",
            )
        return principal

    return dependency
