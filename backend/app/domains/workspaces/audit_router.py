"""The audit trail routes: one page of recorded changes, and the same filters as CSV.

Admin only. The trail names who changed a workspace's access, minted a key or read
state, so reading it is as sensitive as the changes it records. Every filter is
served by the table's keys: the time range by the event id's time prefix, a target
by the target index, and an action or actor as a filter over those reads.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Final, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from webbpulse.audit import AuditEvent as StoredEvent
from webbpulse.audit import AuditQuery, AuditTarget, audit_csv, iter_events
from webbpulse.dynamodb import InvalidStartKey

from ...common import audit
from ...common.core.auth import ADMIN, scopes
from ...common.db.users import UserRepository
from .schemas.audit import AuditEventList

router = APIRouter()

PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 100
EXPORT_LIMIT: Final = 5000
"""The most rows one CSV export carries. A wider range is narrowed with the time filters."""

TARGET_TYPES: Final = (audit.WORKSPACE, audit.API_KEY, audit.DEVICE_GRANT, audit.GITHUB_APP)


def _query(
    since: Optional[datetime],
    until: Optional[datetime],
    actor_id: Optional[str],
    action: Optional[str],
    target_type: Optional[str],
    target_id: Optional[str],
) -> AuditQuery:
    """The listing's filters as a store query, or a 422 for one the trail cannot answer."""
    if action and action not in audit.CATALOGUE:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No such audit action.")
    if bool(target_type) != bool(target_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A target filter needs both target_type and target_id.",
        )
    if target_type and target_type not in TARGET_TYPES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No such audit target type.")
    if since and until and since > until:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="since must not be after until.")
    target = AuditTarget(type=target_type, id=target_id) if target_type and target_id else None
    return AuditQuery(since=since, until=until, actor_id=actor_id or None, action=action or None, target=target)


def _actor_names(events: Iterable[StoredEvent]) -> dict[str, str]:
    """Each person's display name or email, for a session or a key they minted, looked up once per subject."""
    users = UserRepository()
    names: dict[str, str] = {}
    for event in events:
        subject = event.actor.id
        if event.actor.kind == "run_token" or subject in names:
            continue
        try:
            user = users.get(subject)
        except Exception:
            user = None
        if user is not None:
            names[subject] = user.display_name or user.email
    return names


def _render(event: StoredEvent, names: dict[str, str]) -> dict[str, Any]:
    """One stored event as the API renders it."""
    return {
        "event_id": event.event_id,
        "action": event.action,
        "label": audit.CATALOGUE.label(event.action),
        "occurred_at": event.occurred_at,
        "actor_id": event.actor.id,
        "actor_kind": event.actor.kind,
        "actor_name": names.get(event.actor.id),
        "source": event.actor.source,
        "ip": event.actor.ip,
        "amr": list(event.actor.amr),
        "target_type": event.target.type,
        "target_id": event.target.id,
        "target_label": event.target.label,
        "payload": dict(event.payload),
        "before": dict(event.before) if event.before is not None else None,
        "after": dict(event.after) if event.after is not None else None,
    }


Since = Query(default=None, description="Only events at or after this time.")
Until = Query(default=None, description="Only events at or before this time.")
ActorId = Query(default=None, max_length=256, description="Only events this subject made.")
Action = Query(default=None, max_length=128, description="Only this action, such as `workspace.updated`.")
TargetType = Query(default=None, max_length=32, description="With `target_id`, only events on this target.")
TargetId = Query(default=None, max_length=256, description="With `target_type`, only events on this target.")


@router.get("/audit-events", response_model=AuditEventList, dependencies=[Depends(scopes(ADMIN))])
def list_audit_events(
    since: Optional[datetime] = Since,
    until: Optional[datetime] = Until,
    actor_id: Optional[str] = ActorId,
    action: Optional[str] = Action,
    target_type: Optional[str] = TargetType,
    target_id: Optional[str] = TargetId,
    limit: int = Query(default=PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(default=None, max_length=2048),
) -> dict[str, Any]:
    """One page of the audit trail newest first. Admin only.

    `next_cursor` continues the listing under the same filters; a cursor from another
    listing or a changed filter is a 422.
    """
    query = _query(since, until, actor_id, action, target_type, target_id)
    try:
        page = audit.store().list_events(audit.AUDIT_TENANT, query, limit=limit, cursor=cursor)
    except InvalidStartKey as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The cursor does not continue this listing."
        ) from error
    names = _actor_names(page.events)
    return {
        "items": [_render(event, names) for event in page.events],
        "next_cursor": page.next_cursor,
        "event_types": [{"action": name, "label": label} for name, label in audit.CATALOGUE.event_types()],
    }


@router.get(
    "/audit-events/export",
    response_class=Response,
    dependencies=[Depends(scopes(ADMIN))],
    responses={200: {"content": {"text/csv": {}}, "description": "The matching events as CSV."}},
)
def export_audit_events(
    since: Optional[datetime] = Since,
    until: Optional[datetime] = Until,
    actor_id: Optional[str] = ActorId,
    action: Optional[str] = Action,
    target_type: Optional[str] = TargetType,
    target_id: Optional[str] = TargetId,
) -> Response:
    """The matching events as CSV newest first, at most 5000 rows. Admin only.

    Every cell is guarded against spreadsheet formula injection.
    """
    query = _query(since, until, actor_id, action, target_type, target_id)
    events = list(iter_events(audit.store(), audit.AUDIT_TENANT, query, max_items=EXPORT_LIMIT))
    body = audit_csv(events, catalogue=audit.CATALOGUE, actor_names=_actor_names(events))
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-events.csv"', "Cache-Control": "no-store"},
    )
