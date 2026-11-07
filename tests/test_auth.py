import os
import tempfile
import unittest
from asyncio import run
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import jwt

from app.auth import (
    ALLOWED_SCOPES,
    JWT_AUDIENCE,
    JWT_ISSUER,
    Principal,
    _jwt_secret,
    get_current_principal,
    issue_access_token,
    require_scopes,
)
from app.secrets import read_secret


class TokenIssuanceTests(unittest.TestCase):
    def test_issues_short_lived_token_with_expected_identity_and_scopes(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": "test-key-" + "x" * 40}):
            token = issue_access_token("demo-user", {"intents:write", "payments:read"})
            claims = jwt.decode(
                token,
                _jwt_secret(),
                algorithms=["HS256"],
                audience=JWT_AUDIENCE,
                issuer=JWT_ISSUER,
            )

        self.assertEqual(claims["sub"], "demo-user")
        self.assertEqual(set(claims["scope"].split()), {"intents:write", "payments:read"})
        self.assertIn("jti", claims)
        self.assertLessEqual(claims["exp"] - claims["iat"], 15 * 60)

    def test_rejects_unknown_scope(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": "test-key-" + "x" * 40}):
            with self.assertRaises(ValueError):
                issue_access_token("demo-user", {"admin"})

    def test_authenticates_valid_bearer_token(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": "test-key-" + "x" * 40}):
            token = issue_access_token("demo-user", {"intents:write"})
            principal = run(
                get_current_principal(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
                )
            )

        self.assertEqual(principal, Principal("demo-user", frozenset({"intents:write"})))

    def test_rejects_missing_bearer_token(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            run(get_current_principal(None))
        self.assertEqual(raised.exception.status_code, 401)

    def test_rejects_expired_token(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": "test-key-" + "x" * 40}):
            token = jwt.encode(
                {
                    "iss": JWT_ISSUER,
                    "aud": JWT_AUDIENCE,
                    "sub": "demo-user",
                    "scope": "intents:write",
                    "iat": 1,
                    "nbf": 1,
                    "exp": 2,
                    "jti": "expired-token",
                },
                _jwt_secret(),
                algorithm="HS256",
            )
            with self.assertRaises(HTTPException) as raised:
                run(
                    get_current_principal(
                        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
                    )
                )
        self.assertEqual(raised.exception.status_code, 401)

    def test_rejects_missing_scope(self) -> None:
        dependency = require_scopes("policies:read")
        with self.assertRaises(HTTPException) as raised:
            run(dependency(Principal("demo-user", frozenset({"payments:read"}))))
        self.assertEqual(raised.exception.status_code, 403)

    def test_requires_strong_configured_secret(self) -> None:
        with patch.dict(os.environ, {"JWT_SECRET": "short", "JWT_SECRET_FILE": ""}):
            with self.assertRaises(RuntimeError):
                _jwt_secret()

    def test_reads_mounted_secret_file_instead_of_environment_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "jwt_secret"
            secret_path.write_text("file-secret-" + "x" * 40, encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "JWT_SECRET": "environment-secret-" + "y" * 40,
                    "JWT_SECRET_FILE": str(secret_path),
                },
            ):
                self.assertEqual(read_secret("JWT_SECRET"), "file-secret-" + "x" * 40)

    def test_all_known_scopes_are_supported(self) -> None:
        self.assertEqual(ALLOWED_SCOPES, {"intents:write", "payments:read", "policies:read"})


if __name__ == "__main__":
    unittest.main()
