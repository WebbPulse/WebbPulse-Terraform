"""Turning a request's verified claims into the actor recorded on a run.

The triggering principal exists only while the creating request is in flight, so the
route derives it from the claims the guard already verified and the service stores
it with the row. A key and a person are told apart by the `actor_kind` claim
`webbpulse.identity` puts on an API key's claims.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from webbpulse.identity.api_keys import ACTOR_API_KEY, ACTOR_CLAIM

_NAME_CLAIMS = ("display_name", "name", "email")
"""Claims tried in order for a name to render, best first."""


def _first_string(claims: Mapping[str, Any], names: tuple[str, ...]) -> Optional[str]:
    """The first of `names` holding a non-empty string, or `None`."""
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def actor_from_claims(claims: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    """The actor to record on a run created by this request, or `None`.

    `None` when the claims name no subject: an unknown actor is stored as unknown
    rather than attributed to anyone.
    """
    if not claims:
        return None
    subject = str(claims.get("sub", "") or "").strip()
    if not subject:
        return None

    kind = "agent" if str(claims.get(ACTOR_CLAIM, "")) == ACTOR_API_KEY else "user"
    actor: dict[str, Any] = {"kind": kind, "id": subject}
    display_name = _first_string(claims, _NAME_CLAIMS)
    if display_name is not None:
        actor["display_name"] = display_name
    return actor


__all__ = ["actor_from_claims"]
