"""Minting, listing and revoking the `wpk_` keys agents authenticate with.

A thin layer over `webbpulse.identity.api_keys`. The package owns the credential
itself: generating it, hashing it, storing it and verifying it. What is decided
here is who may mint one, what it may carry and whose key a caller may see.

Three rules the package cannot make for us:

A key is minted by a person and never by another key. `claims_or_api_key` renders
a verified key as the same claims object a signed-in person produces, so a route
guarded only by `require_scopes` cannot tell them apart and an agent could mint
its own successor. `ACTOR_CLAIM` is what does tell them apart, and the mint route
refuses a key actor outright.

A key never carries more than its minter holds. The control plane has no
membership store for `claims_or_api_key` to intersect a key against on every
request, so the stored scope set is the key's authority for as long as it lives.
The narrowing therefore has to happen once, at mint time, against the claims the
minter presented.

A key belongs to the person who minted it. The list route answers one caller's
own keys and the revoke route refuses another person's, except for an admin, who
revokes anyone's because a key outliving its owner's access is exactly what an
admin is there to withdraw.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from webbpulse.dynamodb import now_iso
from webbpulse.identity.api_keys import ApiKeyRecord, ApiKeyStore, MintedApiKey, mint
from webbpulse.identity.scopes import claims_scopes

from ...common.core.auth import ALL_SCOPES, RUN_TOKEN_TENANT, RUNNER_SCOPE, api_key_store

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Mapping

    from ...common.composition.settings import Settings

MAX_EXPIRY_YEARS: Final = 2
"""The furthest ahead a key may be set to expire, in years.

A ceiling rather than a required expiry: a key belonging to a service has no
person watching it, and forcing one would trade a quiet expiry for a quiet
outage. Two years is long enough not to be in anybody's way and short enough that
a key minted for a machine that was decommissioned does not outlive the estate.
"""

MAX_KEYS_PER_USER: Final = 25
"""How many live keys one person may hold at once.

Not a security boundary, since every one of them carries at most that person's
own scopes. It is there so a loop that mints instead of reusing is a 409 rather
than an unbounded table.
"""


class KeyNotFound(Exception):
    """No live key with this id, or it belongs to somebody else.

    One exception for both, deliberately. Telling a caller that a key exists but
    is not theirs turns the id into a way to enumerate other people's keys.
    """


class ScopesExceeded(Exception):
    """The requested scopes are not a subset of what the caller holds.

    Carries the scopes that were refused, for a message that says which ones,
    since the caller already knows what they hold and naming them tells them
    nothing new.
    """

    def __init__(self, excess: tuple[str, ...]) -> None:
        """Record the scopes the caller asked for and does not hold."""
        self.excess = excess
        super().__init__(", ".join(excess))


class ExpiryOutOfRange(Exception):
    """The requested expiry is in the past or beyond `MAX_EXPIRY_YEARS`."""


class TooManyKeys(Exception):
    """This caller already holds `MAX_KEYS_PER_USER` live keys."""


def _store(settings: "Settings | None" = None) -> ApiKeyStore:
    """The `api-keys` table, resolved per call rather than captured.

    Resolved per call for the same reason `auth.api_key_store` does it: a test
    that moves the table underneath the settings gets the table it made, not one
    bound to a mocked AWS context that has since closed.
    """
    return api_key_store(settings)


def caller_user_id(claims: "Mapping[str, Any]") -> str:
    """The `sub` a key will be minted under, or `""` when the claims carry none.

    A key acts as the person who minted it, so the subject is the whole of its
    identity and a claims object without one cannot mint.
    """
    return str(claims.get("sub", "") or "").strip()


def caller_scopes(claims: "Mapping[str, Any]") -> tuple[str, ...]:
    """The scopes the caller presented, with `runner` removed.

    `runner` is a run token's scope and never a person's, so it should never be
    on a human's claims at all. Filtered rather than trusted: a key carrying it
    reaches every run's bundle, which holds decrypted variables, and stripping it
    here means a claims bug upstream cannot be laundered into a durable key.
    """
    return tuple(scope for scope in claims_scopes(claims) if scope and scope != RUNNER_SCOPE)


def narrowed_scopes(requested: "Iterable[str] | None", held: "Iterable[str]") -> tuple[str, ...]:
    """The scopes a key may carry: what was asked for, bounded by what is held.

    `None` or an empty request means the caller's whole set, which is the
    ordinary case for an agent standing in for a person.

    Args:
        requested: What the caller asked the key to carry, or `None` for all.
        held: What the caller themselves holds.

    Returns:
        The sorted, de-duplicated scope set, always a subset of `held`.

    Raises:
        ScopesExceeded: The request names a scope the caller does not hold. A
            refusal rather than a silent intersection, because a key quietly
            minted narrower than asked for fails later, somewhere else, in a way
            nobody connects back to this call.
    """
    available = tuple(sorted({scope.strip() for scope in held if scope.strip()}))
    if requested is None:
        return available
    wanted = tuple(sorted({scope.strip() for scope in requested if scope.strip()}))
    if not wanted:
        return available
    excess = tuple(scope for scope in wanted if scope not in available)
    if excess:
        raise ScopesExceeded(excess)
    return wanted


def checked_expiry(expires_at: datetime | None, *, now: datetime | None = None) -> int:
    """`expires_at` as the Unix timestamp the record stores, or `0` for never.

    Raises:
        ExpiryOutOfRange: The moment has already passed, or it is further than
            `MAX_EXPIRY_YEARS` ahead.
    """
    if expires_at is None:
        return 0
    moment = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if moment <= reference:
        raise ExpiryOutOfRange("The expiry has already passed.")
    if moment.timestamp() - reference.timestamp() > MAX_EXPIRY_YEARS * 365 * 24 * 3600:
        raise ExpiryOutOfRange(f"A key may not last longer than {MAX_EXPIRY_YEARS} years.")
    return int(moment.timestamp())


def list_keys(user_id: str, *, settings: "Settings | None" = None) -> list[ApiKeyRecord]:
    """One person's keys, newest first, revoked and expired ones included.

    Revoked and expired ones are kept in the list on purpose: a key a person can
    still see is a key they can recognise and reason about, where one that
    vanished on revocation looks like a key that was never there.

    Run tokens are filtered out. They live in the same table, but their subject
    is a run id rather than a user id, so they would only appear here if a run id
    ever collided with one, and filtering on the scope makes that impossible
    rather than unlikely.
    """
    records = _store(settings).list_for_user(user_id)
    return sorted(
        (record for record in records if RUNNER_SCOPE not in record.scopes),
        key=lambda record: record.created_at,
        reverse=True,
    )


def mint_key(
    *,
    user_id: str,
    name: str,
    requested_scopes: "Iterable[str] | None",
    held_scopes: "Iterable[str]",
    expires_at: datetime | None = None,
    settings: "Settings | None" = None,
) -> MintedApiKey:
    """Mint one key for `user_id`, returning the plaintext once.

    The plaintext is in the return value and nowhere else: only its hash reaches
    the table, so nothing after this call can reproduce it, and the route has to
    put it in that one response.

    Args:
        user_id: The minting person. Becomes the key's `sub`.
        name: A label the owner recognises the key by.
        requested_scopes: What the key should carry, or `None` for everything the
            minter holds.
        held_scopes: What the minter holds, which is the ceiling.
        expires_at: When the key stops working, or `None` for never.
        settings: Settings override, for the suite.

    Returns:
        The plaintext and the stored record, together, once.

    Raises:
        ScopesExceeded: The request names a scope the minter does not hold.
        ExpiryOutOfRange: The expiry is in the past or too far ahead.
        TooManyKeys: The minter already holds `MAX_KEYS_PER_USER` live keys.
    """
    scopes = narrowed_scopes(requested_scopes, held_scopes)
    expiry = checked_expiry(expires_at)

    store = _store(settings)
    live = [record for record in list_keys(user_id, settings=settings) if record.is_usable()]
    if len(live) >= MAX_KEYS_PER_USER:
        raise TooManyKeys(f"This account already holds {MAX_KEYS_PER_USER} live keys.")

    return mint(
        user_id=user_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=scopes,
        name=name,
        expires_at=expiry,
        store=store,
    )


def revoke_key(
    key_id: str,
    *,
    user_id: str,
    is_admin: bool,
    settings: "Settings | None" = None,
) -> ApiKeyRecord:
    """Revoke one key by its id, which is the stored hash.

    Ownership is checked before the write, not after: revoking somebody else's
    key and then reporting a 403 would have already revoked it.

    A run token is never revocable here. Revoking one strands a run that is
    mid-apply, holding a lease on real infrastructure, and the run routes already
    revoke it when the run finishes.

    Args:
        key_id: The stored `key_hash`, which is what the list route renders.
        user_id: The caller, whose own keys these have to be.
        is_admin: Whether the caller may revoke anybody's key.
        settings: Settings override, for the suite.

    Returns:
        The key as it now stands, carrying its `revoked_at`.

    Raises:
        KeyNotFound: No such key, it is a run token, it belongs to somebody else
            and the caller is not an admin, or it was already revoked.
    """
    store = _store(settings)
    existing = store.get(key_id)
    if existing is None or RUNNER_SCOPE in existing.scopes:
        raise KeyNotFound(key_id)
    if not is_admin and existing.user_id != user_id:
        raise KeyNotFound(key_id)
    stamp = now_iso()
    if store.revoke(key_id, revoked_at=stamp) is None:
        raise KeyNotFound(key_id)
    return replace(existing, revoked_at=stamp)


def render_key(record: ApiKeyRecord) -> dict[str, Any]:
    """One key as the API renders it: never the plaintext, never the whole hash.

    `key_id` is the stored hash, which is the only stable handle a caller has and
    is safe to hand out: it is what a presented key hashes to, so holding it
    authenticates as nobody. `prefix` is the clear-text fragment the package
    keeps so a person can tell two keys apart in a list.
    """
    return {
        "key_id": record.key_hash,
        "name": record.name,
        "prefix": record.prefix,
        "scopes": list(record.scopes),
        "created_at": record.created_at,
        "expires_at": record.expires_at or None,
        "last_used_at": record.last_used_at or None,
        "revoked_at": record.revoked_at or None,
    }


__all__ = [
    "ALL_SCOPES",
    "MAX_EXPIRY_YEARS",
    "MAX_KEYS_PER_USER",
    "ApiKeyRecord",
    "ExpiryOutOfRange",
    "KeyNotFound",
    "MintedApiKey",
    "ScopesExceeded",
    "TooManyKeys",
    "caller_scopes",
    "caller_user_id",
    "checked_expiry",
    "list_keys",
    "mint_key",
    "narrowed_scopes",
    "render_key",
    "revoke_key",
]
