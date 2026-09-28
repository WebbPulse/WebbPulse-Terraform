"""The shape of a VCS ingest upload, shared by the webhook consumer and the ingest consumer.

A GitHub App delivery is repacked into an ingest record and an `ingest/<upload id>.tar.gz`
object, and the ingest consumer reads everything it trusts from that record, keyed by the
upload id in the object key, and nothing from the object itself. These are the names both
sides have to agree on.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from ...common.github.webhooks import MAX_DELIVERY_AGE

UPLOAD_ID_PREFIX: Final = "up-"
UPLOAD_ID_PATTERN: Final = r"up-[0-9A-HJKMNP-TV-Z]{26}"
INGEST_PREFIX: Final = "ingest/"
INGEST_CONTENT_TYPE: Final = "application/gzip"
RECORD_TTL: Final = MAX_DELIVERY_AGE + timedelta(days=1)
"""How long an ingest record outlives its delivery. The record is the replay dedupe, so
it outlasts `MAX_DELIVERY_AGE`, past which the webhook route refuses the delivery
outright: a signed body replayed at any age either finds its record or is refused."""

PUSH_EVENT: Final = "push"
PULL_REQUEST_EVENT: Final = "pull_request"

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def crockford(digest: bytes, length: int = 26) -> str:
    """The first `length` Crockford base32 characters of `digest`, ULID alphabet."""
    number = int.from_bytes(digest, "big")
    bits = len(digest) * 8
    return "".join(_CROCKFORD[(number >> (bits - 5 * (index + 1))) & 31] for index in range(length))


def ingest_key(upload_id: str) -> str:
    """The artifacts bucket key an upload's tarball is written to."""
    return f"{INGEST_PREFIX}{upload_id}.tar.gz"


__all__ = [
    "INGEST_CONTENT_TYPE",
    "INGEST_PREFIX",
    "PULL_REQUEST_EVENT",
    "PUSH_EVENT",
    "RECORD_TTL",
    "UPLOAD_ID_PATTERN",
    "UPLOAD_ID_PREFIX",
    "crockford",
    "ingest_key",
]
