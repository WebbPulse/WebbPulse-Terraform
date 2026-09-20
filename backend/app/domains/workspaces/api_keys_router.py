"""The agent API key routes: mint, list and revoke a `wpk_` key.

These are the credential the Go provider and the MCP agents present. A key acts
as the person who minted it, inside their tenant and within their scopes, so
there is no separate agent account to manage and no second authorization path to
keep in step with the first.

Guarded by authentication rather than by a scope. A key can carry at most what
its minter already holds, so requiring a further scope to mint one would gate a
capability the caller has by other means. What is gated is the credential kind:
minting requires a signed-in person, because a key that could mint its own
successor would outlive every revocation of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, status
from webbpulse.identity.scopes import is_api_key_actor

from ...common.core.auth import claims
from . import api_keys_service as service
from .schemas.api_key import ApiKey, ApiKeyCreate, ApiKeyCreated, ApiKeyList

if TYPE_CHECKING:  # pragma: no cover
    from webbpulse.identity.claims import AuthorizerClaims

ADMIN_ROLE: Final = "admin"
"""The role that may revoke another person's key. The same string the identity
hooks put on the `roles` claim for a user carrying `is_admin`."""

KEY_ACTOR_CODE: Final = "API_KEY_ACTOR_FORBIDDEN"
"""The stable code a caller matches on when a key tried to mint another key.

Its own code rather than `INSUFFICIENT_SCOPE`, because no scope fixes it: the
refusal is about which credential arrived, so a frontend offering to re-authorize
for a missing scope would send the caller round a loop that cannot end.
"""

SCOPES_EXCEEDED_CODE: Final = "SCOPES_EXCEEDED"
"""The code for a mint asking for a scope the caller does not hold."""

KEY_LIMIT_CODE: Final = "API_KEY_LIMIT_REACHED"
"""The code for a caller who already holds the maximum number of live keys."""

router = APIRouter()

KeyId = Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
"""A key id is the stored SHA-256 hash, so it is exactly 64 lowercase hex characters."""


def _forbidden(message: str, code: str) -> HTTPException:
    """A 403 in the shared error envelope's shape, carrying a matchable code."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"message": message, "error_code": code})


def _caller(current: "AuthorizerClaims") -> tuple[str, tuple[str, ...], bool]:
    """This request's subject, scopes and admin flag, or a 401 when there is no subject.

    A claims object with no `sub` cannot own a key, and minting one under an empty
    subject would produce a credential nobody can list or revoke.
    """
    user_id = service.caller_user_id(current)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Authentication is required.", "error_code": "UNAUTHORIZED"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    roles = current.get("roles") or []
    is_admin = ADMIN_ROLE in [str(role) for role in roles]
    return user_id, service.caller_scopes(current), is_admin


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_api_key(payload: ApiKeyCreate, current: "AuthorizerClaims" = Depends(claims)) -> dict[str, Any]:
    """Mint one key and return its plaintext, once.

    The response body is the only place the plaintext ever exists outside the
    caller's own storage: the table holds its SHA-256 and nothing else, so a
    caller that loses it mints another key rather than recovering this one.

    Requires a signed-in person. A key presenting itself here is refused with
    `API_KEY_ACTOR_FORBIDDEN`, because a key able to mint its successor would
    survive being revoked.
    """
    if is_api_key_actor(current):
        raise _forbidden("An API key may not mint another API key.", KEY_ACTOR_CODE)

    user_id, held, _ = _caller(current)
    try:
        minted = service.mint_key(
            user_id=user_id,
            name=payload.name,
            requested_scopes=payload.scopes,
            held_scopes=held,
            expires_at=payload.expires_at,
        )
    except service.ScopesExceeded as error:
        raise _forbidden(
            f"A key cannot carry scopes you do not hold: {', '.join(error.excess)}.",
            SCOPES_EXCEEDED_CODE,
        ) from error
    except service.ExpiryOutOfRange as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    except service.TooManyKeys as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(error), "error_code": KEY_LIMIT_CODE},
        ) from error

    return {**service.render_key(minted.record), "key": minted.plaintext}


@router.get("/api-keys", response_model=ApiKeyList)
def list_api_keys(current: "AuthorizerClaims" = Depends(claims)) -> dict[str, Any]:
    """The caller's own keys, newest first. Never a plaintext.

    Revoked and expired keys stay in the list so a person can recognise one they
    retired, and run tokens are left out: they live in the same table but belong
    to a run rather than to anybody.
    """
    user_id, _, _ = _caller(current)
    return {"items": [service.render_key(record) for record in service.list_keys(user_id)]}


@router.delete("/api-keys/{key_id}", response_model=ApiKey)
def revoke_api_key(key_id: str = KeyId, current: "AuthorizerClaims" = Depends(claims)) -> dict[str, Any]:
    """Revoke one key, taking effect on the caller's next request.

    Returns the key as it was rather than 204, so the caller can render what it
    just retired without a second read.

    The caller's own key, or anybody's when the caller is an admin. Somebody
    else's key reads as a 404 rather than a 403, because a 403 would confirm the
    id exists and turn the list into a way to enumerate other people's keys.
    """
    user_id, _, is_admin = _caller(current)
    try:
        revoked = service.revoke_key(key_id, user_id=user_id, is_admin=is_admin)
    except service.KeyNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such API key.") from error
    return service.render_key(revoked)
