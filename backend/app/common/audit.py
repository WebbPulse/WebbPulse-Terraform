"""The control plane's durable audit trail, on `webbpulse.audit`.

Every change worth reconstructing later is recorded here after it lands: workspace
create, update and delete, the settings that decide what a run may reach or apply,
API key mint and revoke, variable writes, state downloads and run decisions. The
plane serves one organization, so every event lives in one tenant partition and a
workspace or key is the event's target, which the table's target index lists.

Recording is best effort: a failed write is logged as `audit_write_failed` and never
fails the change it describes. Each action's payload passes through an allowlist,
so a variable's value or a key's plaintext cannot reach the table whatever a caller
hands in.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any, Final, Optional

from fastapi import Request
from webbpulse.audit import (
    AuditAction,
    AuditActor,
    AuditCatalogue,
    AuditEvent,
    AuditLogStore,
    AuditRecorder,
    AuditTarget,
    DynamoAuditLogStore,
)
from webbpulse.http import client_ip
from webbpulse.identity.scopes import is_api_key_actor

from .composition.settings import Settings
from .core.auth import is_run_api_claims
from .db import repositories
from .db.tables import AUDIT_TARGET_INDEX

_log = logging.getLogger(__name__)

AUDIT_TENANT: Final = "webbpulse"
"""The one tenant partition every event is recorded under, since the plane serves one organization."""

RETENTION: Final = timedelta(days=365)
"""How long an event is kept before the table's TTL removes it, the retention `webbpulse.audit` documents."""

TFE_PREFIX: Final = "/api/v2"
"""The path prefix the Terraform CLI and go-tfe call, which marks an event's source as the CLI."""

WORKSPACE: Final = "workspace"
API_KEY: Final = "api_key"
DEVICE_GRANT: Final = "device_grant"
GITHUB_APP: Final = "github_app"

WORKSPACE_CREATED: Final = "workspace.created"
WORKSPACE_UPDATED: Final = "workspace.updated"
WORKSPACE_DELETED: Final = "workspace.deleted"
AUTO_APPLY_CHANGED: Final = "workspace.auto_apply_changed"
PLAN_ACCESS_CHANGED: Final = "workspace.plan_access_changed"
REMOTE_STATE_CHANGED: Final = "workspace.remote_state_changed"
RUN_API_SCOPES_CHANGED: Final = "workspace.run_api_scopes_changed"
API_KEY_CREATED: Final = "api_key.created"
API_KEY_REVOKED: Final = "api_key.revoked"
VARIABLE_WRITTEN: Final = "variable.written"
VARIABLE_DELETED: Final = "variable.deleted"
STATE_DOWNLOADED: Final = "state_version.downloaded"
RUN_CONFIRMED: Final = "run.confirmed"
RUN_DISCARDED: Final = "run.discarded"
DEVICE_GRANT_OPENED: Final = "device_grant.opened"
AWS_CONNECTED: Final = "workspace.aws_connected"
GITHUB_APP_CREATED: Final = "github.app_created"
GITHUB_WEBHOOK_SYNCED: Final = "github.webhook_synced"
GITHUB_INSTALLATION_RECORDED: Final = "github.installation_recorded"
GITHUB_INSTALLATION_REMOVED: Final = "github.installation_removed"

AUTO_APPLY_FIELDS: Final = frozenset({"auto_apply"})
PLAN_ACCESS_FIELDS: Final = frozenset({"plan_role_arn", "plan_assume_role_arns", "plan_secret_arns"})
REMOTE_STATE_FIELDS: Final = frozenset({"global_remote_state", "remote_state_consumer_ids"})
RUN_API_SCOPES_FIELDS: Final = frozenset({"run_api_token_scopes"})

SETTINGS_ACTIONS: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    (AUTO_APPLY_CHANGED, AUTO_APPLY_FIELDS),
    (PLAN_ACCESS_CHANGED, PLAN_ACCESS_FIELDS),
    (REMOTE_STATE_CHANGED, REMOTE_STATE_FIELDS),
    (RUN_API_SCOPES_CHANGED, RUN_API_SCOPES_FIELDS),
)
"""Each settings action and the workspace fields it records, apart from the general update."""

WORKSPACE_FIELDS: Final = frozenset(
    {
        "name",
        "engine",
        "engine_version",
        "run_role_arn",
        "pending_run_role_arn",
        "working_directory",
        "description",
        "vcs_repo",
        "tracked_branch",
        "trigger_patterns",
        "speculative_plans",
        "file_triggers_enabled",
        "project_id",
    }
)
"""The workspace fields a general update records. The settings fields have actions of their own."""

CATALOGUE: Final = AuditCatalogue(
    {
        WORKSPACE_CREATED: AuditAction(
            "Workspace created",
            WORKSPACE_FIELDS | AUTO_APPLY_FIELDS | PLAN_ACCESS_FIELDS | REMOTE_STATE_FIELDS | RUN_API_SCOPES_FIELDS,
        ),
        WORKSPACE_UPDATED: AuditAction("Workspace updated", WORKSPACE_FIELDS),
        WORKSPACE_DELETED: AuditAction("Workspace deleted", frozenset({"force"})),
        AUTO_APPLY_CHANGED: AuditAction("Auto-apply changed", AUTO_APPLY_FIELDS),
        PLAN_ACCESS_CHANGED: AuditAction("Plan access changed", PLAN_ACCESS_FIELDS),
        REMOTE_STATE_CHANGED: AuditAction("Remote state sharing changed", REMOTE_STATE_FIELDS),
        RUN_API_SCOPES_CHANGED: AuditAction("Run API token scopes changed", RUN_API_SCOPES_FIELDS),
        API_KEY_CREATED: AuditAction("API key created", frozenset({"scopes", "expires_at", "no_expiry"})),
        API_KEY_REVOKED: AuditAction("API key revoked", frozenset({"owner_id"})),
        VARIABLE_WRITTEN: AuditAction("Variable written", frozenset({"key", "category"})),
        VARIABLE_DELETED: AuditAction("Variable deleted", frozenset({"key", "category"})),
        STATE_DOWNLOADED: AuditAction("State downloaded", frozenset({"state_version_id"})),
        RUN_CONFIRMED: AuditAction("Run confirmed", frozenset({"run_id"})),
        RUN_DISCARDED: AuditAction("Run discarded", frozenset({"run_id"})),
        DEVICE_GRANT_OPENED: AuditAction("Device grant opened", frozenset({"client_id", "scopes"})),
        AWS_CONNECTED: AuditAction("AWS credentials connected", frozenset({"account_id", "role_arn", "pending"})),
        GITHUB_APP_CREATED: AuditAction("GitHub App created", frozenset({"app_slug"})),
        GITHUB_WEBHOOK_SYNCED: AuditAction("GitHub App webhook synced", frozenset()),
        GITHUB_INSTALLATION_RECORDED: AuditAction("GitHub App installation recorded", frozenset({"installation_id"})),
        GITHUB_INSTALLATION_REMOVED: AuditAction("GitHub App installation removed", frozenset({"installation_id"})),
    }
)
"""Every action the plane records, its label, and the payload fields each may carry."""


def store(settings: Settings | None = None) -> AuditLogStore:
    """The audit table's store, built per call so a test's moved settings are read."""
    return DynamoAuditLogStore(repositories.audit(settings), target_index=AUDIT_TARGET_INDEX)


def recorder(settings: Settings | None = None) -> AuditRecorder:
    """The best effort recorder over the audit table, with the trail's retention."""
    return AuditRecorder(store(settings), CATALOGUE, retention=RETENTION)


def _amr(claims: Mapping[str, Any]) -> tuple[str, ...]:
    """The session's authentication methods, from a list or a space separated claim."""
    raw = claims.get("amr")
    if isinstance(raw, str):
        return tuple(part for part in raw.split() if part)
    if isinstance(raw, list | tuple):
        return tuple(str(part) for part in raw if str(part))
    return ()


def actor_from(request: Optional[Request], claims: Optional[Mapping[str, Any]]) -> AuditActor:
    """Who made this request: the subject, its credential kind, the client and the address.

    A run's API token is `run_token`, any other `wpk_` key `api_key`, and a signed-in
    session `user`. The source is `cli` on the `tfe.v2` surface, `api` for a key
    elsewhere and `web` for a session.
    """
    current = claims or {}
    subject = str(current.get("sub", "") or "").strip() or "unknown"
    if is_run_api_claims(current):
        kind = "run_token"
    elif is_api_key_actor(current):
        kind = "api_key"
    else:
        kind = "user"
    path = request.url.path if request is not None else ""
    if path.startswith(TFE_PREFIX):
        source = "cli"
    elif kind == "user":
        source = "web"
    else:
        source = "api"
    ip = client_ip(request) if request is not None else ""
    return AuditActor(id=subject, kind=kind, source=source, ip=ip, amr=_amr(current))


def workspace_target(workspace_id: str, name: str = "") -> AuditTarget:
    """A workspace as an event's target, labelled by its name where known."""
    return AuditTarget(type=WORKSPACE, id=workspace_id, label=name)


def record(
    action: str,
    *,
    request: Optional[Request],
    claims: Optional[Mapping[str, Any]],
    target: AuditTarget,
    payload: Optional[Mapping[str, Any]] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    actor: Optional[AuditActor] = None,
) -> Optional[AuditEvent]:
    """Record one event after its change landed, answering `None` when it could not be built or written.

    `actor` names who acted when there is no signed-in request to read it from, such
    as a CLI's token exchange or a queue consumer.
    """
    try:
        return recorder().record(
            AUDIT_TENANT,
            action,
            actor=actor or actor_from(request, claims),
            target=target,
            payload=payload,
            before=before,
            after=after,
        )
    except Exception:
        _log.exception("audit_write_failed", extra={"audit_action": action, "tenant_id": AUDIT_TENANT})
        return None


def record_run_decision(
    action: str,
    *,
    request: Optional[Request],
    claims: Optional[Mapping[str, Any]],
    run: Mapping[str, Any],
) -> Optional[AuditEvent]:
    """Record a run's confirm or discard against its workspace, naming the run but never its free text comment."""
    return record(
        action,
        request=request,
        claims=claims,
        target=workspace_target(str(run.get("workspace_id", "") or "unknown")),
        payload={"run_id": str(run.get("run_id", ""))},
    )


def _comparable(value: Any) -> Any:
    """A stored value as an edit compares it, a null and an empty value being the same."""
    return value or None


def record_workspace_changes(
    *,
    request: Optional[Request],
    claims: Optional[Mapping[str, Any]],
    workspace_id: str,
    name: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fields: Iterable[str],
) -> None:
    """Record a workspace edit: the general fields as one update, each settings group as its own action.

    Only a field whose value moved is recorded, so resending the stored value writes nothing.
    """
    moved = [field for field in fields if _comparable(before.get(field)) != _comparable(after.get(field))]
    target = workspace_target(workspace_id, name)
    groups = ((WORKSPACE_UPDATED, WORKSPACE_FIELDS), *SETTINGS_ACTIONS)
    for action, group in groups:
        changed = [field for field in moved if field in group]
        if not changed:
            continue
        record(
            action,
            request=request,
            claims=claims,
            target=target,
            before={field: before.get(field) for field in changed},
            after={field: after.get(field) for field in changed},
        )


__all__ = [
    "API_KEY",
    "API_KEY_CREATED",
    "API_KEY_REVOKED",
    "AUDIT_TENANT",
    "AUTO_APPLY_CHANGED",
    "AWS_CONNECTED",
    "CATALOGUE",
    "DEVICE_GRANT",
    "DEVICE_GRANT_OPENED",
    "GITHUB_APP",
    "GITHUB_APP_CREATED",
    "GITHUB_INSTALLATION_RECORDED",
    "GITHUB_INSTALLATION_REMOVED",
    "GITHUB_WEBHOOK_SYNCED",
    "PLAN_ACCESS_CHANGED",
    "REMOTE_STATE_CHANGED",
    "RETENTION",
    "RUN_API_SCOPES_CHANGED",
    "RUN_CONFIRMED",
    "RUN_DISCARDED",
    "STATE_DOWNLOADED",
    "VARIABLE_DELETED",
    "VARIABLE_WRITTEN",
    "WORKSPACE",
    "WORKSPACE_CREATED",
    "WORKSPACE_DELETED",
    "WORKSPACE_UPDATED",
    "actor_from",
    "record",
    "record_run_decision",
    "record_workspace_changes",
    "recorder",
    "store",
    "workspace_target",
]
