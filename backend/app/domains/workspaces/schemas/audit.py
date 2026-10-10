"""Response models for the audit trail routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class AuditEvent(BaseModel):
    """One recorded change: who made it, when, to what, and what moved."""

    event_id: str
    """The event's ULID, which also orders the trail newest first."""
    action: str
    """The stable action name, such as `workspace.updated`."""
    label: str
    """The action's human label, such as `Workspace updated`."""
    occurred_at: datetime
    actor_id: str
    """The subject that made the change: a user id, or a run id for a run's token."""
    actor_kind: str
    """`user`, `api_key` or `run_token`."""
    actor_name: Optional[str] = None
    """The person's display name or email where the subject is a known user."""
    source: str
    """`web`, `api` or `cli`."""
    ip: str
    amr: list[str]
    """The session's authentication methods, empty for a key."""
    target_type: str
    """`workspace` or `api_key`."""
    target_id: str
    target_label: str
    payload: dict[str, Any]
    """The action's allowlisted fields. A variable's value or a key's plaintext is never among them."""
    before: Optional[dict[str, Any]] = None
    """The changed fields' values before an edit."""
    after: Optional[dict[str, Any]] = None
    """The changed fields' values after an edit."""


class AuditEventType(BaseModel):
    """One action the trail records, for a filter."""

    action: str
    label: str


class AuditEventList(BaseModel):
    """One page of the trail, newest first."""

    items: list[AuditEvent]
    next_cursor: Optional[str] = None
    """Pass back as `cursor` with the same filters for the next page; absent on the last."""
    event_types: list[AuditEventType]
    """Every action the trail records, so a filter can offer them all."""
