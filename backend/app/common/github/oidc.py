"""Verifying a GitHub Actions OIDC token, shared by every domain a workflow calls.

A workflow authenticates with the token GitHub mints for the job. The signature is
checked against GitHub's published keys, then the issuer, the audience, `exp` and
`nbf`, and finally that every claim the caller trusts is present and non blank.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Final, Iterable

GITHUB_ISSUER: Final = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URI: Final = f"{GITHUB_ISSUER}/.well-known/jwks"

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class InvalidActionsToken(Exception):
    """The bearer is missing, fails verification, or lacks a required claim."""


@lru_cache(maxsize=4)
def verifier(audience: str) -> Any:
    """One JWKS verifier per audience per process, so GitHub's keys are cached."""
    from webbpulse.identity.verifier import JwksVerifier

    return JwksVerifier(issuer=GITHUB_ISSUER, audience=audience, jwks_uri=GITHUB_JWKS_URI)


def verify_actions_token(token: str, *, audience: str, required: Iterable[str]) -> dict[str, Any]:
    """The verified claims of a GitHub Actions OIDC token.

    Raises:
        InvalidActionsToken: No token, a token that fails verification, or one
            missing any of `required`.
    """
    from webbpulse.identity.service import InvalidToken

    if not token:
        raise InvalidActionsToken("no bearer token")
    try:
        claims = verifier(audience).verify(token, expected_type=None)
    except InvalidToken as error:
        raise InvalidActionsToken(str(error)) from error
    missing = [name for name in required if not str(claims.get(name, "") or "").strip()]
    if missing:
        raise InvalidActionsToken(f"the token lacks {', '.join(missing)}")
    return dict(claims)


def crockford(digest: bytes, length: int = 26) -> str:
    """The first `length` Crockford base32 characters of `digest`, ULID alphabet."""
    number = int.from_bytes(digest, "big")
    bits = len(digest) * 8
    return "".join(_CROCKFORD[(number >> (bits - 5 * (index + 1))) & 31] for index in range(length))


__all__ = [
    "GITHUB_ISSUER",
    "GITHUB_JWKS_URI",
    "InvalidActionsToken",
    "crockford",
    "verifier",
    "verify_actions_token",
]
