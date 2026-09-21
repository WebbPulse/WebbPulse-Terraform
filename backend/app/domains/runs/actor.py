"""Turning a request's verified claims into the actor stamped on a run row.

A run records who triggered it and how, and the information only exists while the
request that created it is in flight: nothing recoverable ties a row written later
back to a principal. So the derivation happens at the route, from the claims the
guard already resolved, and the result travels into the service as data.

Three kinds, taken from what the credential actually proves rather than from what
would read nicely. `webbpulse.identity` already marks an API key's claims with
`actor_kind`, so a key and a person are told apart by the package's own signal
instead of by a second guess here. Anything that reaches run creation without a
principal at all, which is every internal path and the runner token, is `system`:
a run the control plane started for itself is a true statement, and attributing it
to whichever person happened to be nearby would not be.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from webbpulse.identity.api_keys import ACTOR_API_KEY, ACTOR_CLAIM

SYSTEM_ACTOR: dict[str, Any] = {"kind": "system"}
"""The actor for a run with no principal behind it. A constant rather than a
built dict, so every internal path stamps the same shape."""

_NAME_CLAIMS = ("display_name", "name", "email")
"""Claims tried in order for something to render, best first.

`display_name` is what this product's own tokens carry, `name` is the OIDC
standard spelling and `email` is the last resort: an address is a poor label but a
better one than a bare uuid.
"""


def _first_string(claims: Mapping[str, Any], names: tuple[str, ...]) -> Optional[str]:
    """The first of `names` holding a non-empty string, or `None`."""
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def actor_from_claims(claims: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """The actor to stamp on a run created by this request.

    Never raises and never returns `None`. A request whose claims are absent,
    empty or subject-less is a `system` actor, which keeps run creation working on
    every path that has no person behind it rather than failing the create over
    attribution.

    Args:
        claims: The verified claims for the creating request, or `None` where the
            caller has none, as an internal path does.

    Returns:
        A dict shaped like `RunActor`, always carrying `kind`.
    """
    if not claims:
        return dict(SYSTEM_ACTOR)

    subject = str(claims.get("sub", "") or "").strip()
    if not subject:
        return dict(SYSTEM_ACTOR)

    kind = "agent" if str(claims.get(ACTOR_CLAIM, "")) == ACTOR_API_KEY else "user"
    actor: dict[str, Any] = {"kind": kind, "id": subject}

    display_name = _first_string(claims, _NAME_CLAIMS)
    if display_name is not None:
        actor["display_name"] = display_name
    return actor


def actor_columns(actor: Mapping[str, Any]) -> dict[str, Any]:
    """The flat attributes an actor occupies on a run row.

    Flat rather than a nested map because the `by_recency` index projects
    individual attributes and DynamoDB cannot project into a nested document, so
    the kind and the name a cross-workspace list renders have to be top level to
    come back from that index at all.

    Empty strings are dropped rather than stored: an absent attribute and an empty
    one would render the same way, and not storing one keeps the projected index
    smaller.
    """
    columns: dict[str, Any] = {"actor_kind": str(actor.get("kind", "system"))}
    for source, column in (("id", "actor_id"), ("display_name", "actor_display_name")):
        value = actor.get(source)
        if isinstance(value, str) and value.strip():
            columns[column] = value.strip()
    return columns


def actor_from_row(item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The actor a stored run row carries, or `None` for a row that has none.

    Every run written before attribution shipped is such a row, and there is no
    backfill: the principal was never recorded, so inventing one would be a lie
    that reads like an audit trail. `None` is the honest answer and the model
    allows it.
    """
    kind = str(item.get("actor_kind", "") or "").strip()
    if not kind:
        return None

    actor: dict[str, Any] = {"kind": kind}
    for column, field in (("actor_id", "id"), ("actor_display_name", "display_name")):
        value = item.get(column)
        if isinstance(value, str) and value.strip():
            actor[field] = value.strip()
    return actor


__all__ = ["SYSTEM_ACTOR", "actor_columns", "actor_from_claims", "actor_from_row"]
