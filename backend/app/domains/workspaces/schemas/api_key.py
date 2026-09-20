"""Request and response models for the agent API key routes."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ApiKeyCreate(BaseModel):
    """A request to mint one key.

    `scopes` is optional and bounded rather than free: omitting it mints a key
    carrying everything the caller holds, and naming a scope the caller does not
    hold is refused rather than quietly dropped.
    """

    name: str = Field(min_length=1, max_length=120)
    """A label the owner recognises the key by in a list. Not unique, and not an
    identifier: the key id is the stored hash."""
    scopes: Optional[list[str]] = Field(default=None, max_length=32)
    """What the key may carry, bounded by the caller's own scopes. `null` or an
    empty list means everything the caller holds."""
    expires_at: Optional[datetime] = None
    """When the key stops working. `null` mints one that never expires, which is
    right for a key belonging to a service rather than a person."""


class ApiKey(BaseModel):
    """A stored key as every route but the mint renders it. Never the plaintext."""

    key_id: str
    """The stored SHA-256 hash, which is the handle every other route takes. Safe
    to render: a presented key hashes to this, so holding it authenticates as
    nobody."""
    name: str
    prefix: str
    """The clear-text fragment the package keeps, so two keys can be told apart."""
    scopes: list[str]
    created_at: str
    expires_at: Optional[int] = None
    """Unix seconds, or `null` for a key that never expires."""
    last_used_at: Optional[str] = None
    revoked_at: Optional[str] = None


class ApiKeyCreated(ApiKey):
    """A freshly minted key, carrying the one and only sight of its plaintext.

    Returned by `POST /api/v1/api-keys` and by nothing else. The plaintext is not
    stored, so a caller that loses it has to mint another key.
    """

    key: str
    """The plaintext, shown once. It is never recoverable after this response."""


class ApiKeyList(BaseModel):
    """The caller's own keys, newest first."""

    items: list[ApiKey]
