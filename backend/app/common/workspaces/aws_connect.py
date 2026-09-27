"""The one-time connect token a Quick setup stack reports back with, and what it records.

Quick setup hands out a CloudFormation link whose stack carries a custom resource.
CloudFormation publishes the resource's lifecycle to this environment's SNS topic,
and the runs function answers it. The request names the stack, and so the account,
and the role the stack created, so nobody has to type an account id or copy an ARN.

What ties a stack to a workspace is the token in the link: random, stored only as
its SHA-256, single use and short lived. A create carrying a token that matches
stages the role with the same rules a person's edit follows (taken at once when the
workspace has no role or already runs as it, staged beside a working role
otherwise), consumes the token and records the connection for the UI to read.

The workspaces function issues tokens and the runs function consumes them, so this
module lives in `common`, since neither domain may import the other.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso

from ..composition.settings import Settings, get_settings
from ..db import repositories
from .reads import WORKSPACE_ID_PREFIX, WorkspaceNotFound, get_workspace

TOKEN_PREFIX: Final = "wct_"
"""Marks a connect token, so one pasted anywhere reads as what it is."""

TOKEN_PATTERN: Final = r"^wct_[A-Za-z0-9_-]{43}$"
"""A token as `issue_token` mints it: the prefix and 32 random bytes, base64url."""

TOKEN_TTL_SECONDS: Final = 3600
"""How long a link can wait before its stack is created. Matches the template URL."""

PHYSICAL_ID_PREFIX: Final = "wpc-"
"""The custom resource physical id of a stack that connected a workspace."""

TOKEN_HASH_ATTRIBUTE: Final = "aws_connect_token_hash"
TOKEN_EXPIRES_ATTRIBUTE: Final = "aws_connect_token_expires_at"
CONNECTION_ATTRIBUTE: Final = "aws_connection"

CHECK_FIELDS: Final = ("run_role_checked_at", "run_role_account_id")
"""The recorded check outcome, which belongs to one role and goes when it changes."""

ConnectOutcome = Literal["connected", "repeat", "invalid", "expired"]
"""What a stack's create did: connected, a retry of one that did, or refused."""

_STACK_ARN = re.compile(r"^arn:(aws[a-z-]*):cloudformation:[a-z0-9-]+:(\d{12}):stack/[^/]+/.+$")


def run_role_name(workspace_id: str, *, settings: Settings | None = None) -> str:
    """The role name for one workspace, inside the runner's AssumeRole grant.

    The `ws-` prefix is dropped so the ULID alone follows the stack prefix, which
    keeps the name inside the IAM ceiling of sixty four characters.
    """
    resolved = settings or get_settings()
    return f"{resolved.RUN_ROLE_NAME_PREFIX}{workspace_id.removeprefix(WORKSPACE_ID_PREFIX)}"


def hash_token(token: str) -> str:
    """The stored form of a token. A plain hash suffices: the token carries 256 random bits."""
    return hashlib.sha256(token.encode()).hexdigest()


def stack_account(stack_id: str) -> tuple[str, str] | None:
    """The partition and account a CloudFormation stack ARN names, or `None`."""
    match = _STACK_ARN.match(stack_id)
    return (match.group(1), match.group(2)) if match else None


def expected_role_arn(workspace_id: str, partition: str, account_id: str, *, settings: Settings) -> str:
    """The only role ARN a stack in `account_id` may report for this workspace."""
    return f"arn:{partition}:iam::{account_id}:role/{run_role_name(workspace_id, settings=settings)}"


def _expires_at(now: datetime) -> str:
    """The expiry stamp for a token minted at `now`."""
    return (now + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat()


def issue_token(workspace_id: str, *, settings: Settings | None = None) -> tuple[str, str]:
    """Mint a connect token for one workspace, replacing any earlier one.

    Only the hash is stored, and the connection reads `waiting` until a stack
    reports back. Returns the plaintext token, which exists only in the link, and
    its expiry.

    Raises:
        WorkspaceNotFound: No such workspace.
    """
    resolved = settings or get_settings()
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    expires_at = _expires_at(now)
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression="SET #hash = :hash, #expires = :expires, #connection = :connection",
            expression_names={
                "#hash": TOKEN_HASH_ATTRIBUTE,
                "#expires": TOKEN_EXPIRES_ATTRIBUTE,
                "#connection": CONNECTION_ATTRIBUTE,
            },
            expression_values={
                ":hash": hash_token(token),
                ":expires": expires_at,
                ":connection": {"status": "waiting", "requested_at": now.isoformat(), "expires_at": expires_at},
            },
            condition=Attr("workspace_id").exists(),
        )
    except ConditionFailed as error:
        raise WorkspaceNotFound(workspace_id) from error
    return token, expires_at


@dataclass(frozen=True)
class ConnectResult:
    """What a stack's create request did to the workspace."""

    outcome: ConnectOutcome
    physical_id: str = ""
    pending: bool = False
    """True when the role was staged beside a working one rather than taken at once."""


def _is_repeat(connection: dict[str, Any], stack_id: str, request_id: str) -> bool:
    """Whether this request already connected the workspace, so a retry only re-answers."""
    return (
        connection.get("status") == "connected"
        and connection.get("stack_id") == stack_id
        and connection.get("request_id") == request_id
    )


def _expired(expires_at: str) -> bool:
    """Whether a stored expiry has passed. An unreadable one counts as passed."""
    try:
        return datetime.fromisoformat(expires_at) <= datetime.now(UTC)
    except ValueError:
        return True


def _mark_expired(workspace_id: str, token_hash: str, *, settings: Settings) -> None:
    """Drop an expired token and show the connection expired, unless a newer token replaced it."""
    try:
        repositories.workspaces(settings).update(
            {"workspace_id": workspace_id},
            update_expression="SET #connection.#status = :expired REMOVE #hash, #expires",
            expression_names={
                "#connection": CONNECTION_ATTRIBUTE,
                "#status": "status",
                "#hash": TOKEN_HASH_ATTRIBUTE,
                "#expires": TOKEN_EXPIRES_ATTRIBUTE,
            },
            expression_values={":expired": "expired"},
            condition=Attr(TOKEN_HASH_ATTRIBUTE).eq(token_hash),
        )
    except ConditionFailed:
        pass


def connect(
    workspace_id: str,
    *,
    token: str,
    role_arn: str,
    account_id: str,
    stack_id: str,
    request_id: str,
    physical_id: str | None = None,
    settings: Settings | None = None,
) -> ConnectResult:
    """Consume a stack's token and stage the role it created.

    The caller has already checked that `role_arn` is the one this workspace's
    stack in `account_id` creates. A retry of a request that connected answers
    `repeat` with the same physical id. The write is conditional on the token
    hash, so two deliveries of one token cannot both consume it.
    """
    resolved = settings or get_settings()
    try:
        workspace = get_workspace(workspace_id, settings=resolved)
    except WorkspaceNotFound:
        return ConnectResult("invalid")
    connection = dict(workspace.get(CONNECTION_ATTRIBUTE) or {})
    if _is_repeat(connection, stack_id, request_id):
        return ConnectResult("repeat", str(connection.get("physical_id", "")), bool(connection.get("pending")))

    stored = str(workspace.get(TOKEN_HASH_ATTRIBUTE) or "")
    presented = hash_token(token)
    if not stored or not hmac.compare_digest(stored, presented):
        return ConnectResult("invalid")
    if _expired(str(workspace.get(TOKEN_EXPIRES_ATTRIBUTE) or "")):
        _mark_expired(workspace_id, stored, settings=resolved)
        return ConnectResult("expired")

    current = str(workspace.get("run_role_arn") or "")
    pending = bool(current) and current != role_arn
    physical = physical_id or f"{PHYSICAL_ID_PREFIX}{new_ulid()}"
    now = now_iso()
    record = {
        "status": "connected",
        "account_id": account_id,
        "role_arn": role_arn,
        "pending": pending,
        "stack_id": stack_id,
        "request_id": request_id,
        "physical_id": physical,
        "reported_at": now,
        "verification": "pending",
    }
    names = {
        "#connection": CONNECTION_ATTRIBUTE,
        "#hash": TOKEN_HASH_ATTRIBUTE,
        "#expires": TOKEN_EXPIRES_ATTRIBUTE,
    }
    values: dict[str, Any] = {":connection": record, ":role": role_arn, ":now": now}
    removals = ["#hash", "#expires"]
    if pending:
        assignments = "#connection = :connection, pending_run_role_arn = :role, updated_at = :now"
    else:
        assignments = "#connection = :connection, run_role_arn = :role, updated_at = :now"
        removals.append("pending_run_role_arn")
        if current != role_arn:
            removals.extend(CHECK_FIELDS)
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression=f"SET {assignments} REMOVE {', '.join(removals)}",
            expression_names=names,
            expression_values=values,
            condition=Attr(TOKEN_HASH_ATTRIBUTE).eq(stored),
        )
    except ConditionFailed:
        again = dict(get_workspace(workspace_id, settings=resolved).get(CONNECTION_ATTRIBUTE) or {})
        if _is_repeat(again, stack_id, request_id):
            return ConnectResult("repeat", str(again.get("physical_id", "")), bool(again.get("pending")))
        return ConnectResult("invalid")
    return ConnectResult("connected", physical, pending)


def record_run(workspace_id: str, request_id: str, run_id: str, *, settings: Settings | None = None) -> None:
    """Note the verification run on the connection a request recorded, so the UI can link it."""
    resolved = settings or get_settings()
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression="SET #connection.run_id = :run",
            expression_names={"#connection": CONNECTION_ATTRIBUTE},
            expression_values={":run": run_id},
            condition=Attr(f"{CONNECTION_ATTRIBUTE}.request_id").eq(request_id),
        )
    except ConditionFailed:
        pass


def fail_verification(workspace_id: str, request_id: str, error: str, *, settings: Settings | None = None) -> None:
    """Show the connection a request recorded as failed, when its verification run never started."""
    resolved = settings or get_settings()
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression=(
                "SET #connection.verification = :failed, #connection.verification_error = :error, "
                "#connection.verified_at = :now"
            ),
            expression_names={"#connection": CONNECTION_ATTRIBUTE},
            expression_values={":failed": "failed", ":error": error, ":now": now_iso()},
            condition=Attr(f"{CONNECTION_ATTRIBUTE}.request_id").eq(request_id),
        )
    except ConditionFailed:
        pass


VERIFIED_RUN_STATUSES: Final = frozenset({"planned_and_finished", "applied", "discarded"})
"""Terminal statuses a run reaches only after its plan finished cleanly on the role."""

FAILED_RUN_STATUSES: Final = frozenset({"errored", "cancelled"})
"""Terminal statuses of a run that ended without proving the role."""

UNSETTLED_VERIFICATIONS: Final = ("pending", "failed")
"""Verifications a later run on the same role may still settle."""


def _verification_failure(run: dict[str, Any]) -> str:
    """The words a failed verification shows, from the run that failed it."""
    if str(run.get("status", "")) == "cancelled":
        return "The verification run was cancelled before it finished."
    return str(run.get("error") or "") or "The verification run errored."


def record_verification(run: dict[str, Any], *, settings: Settings | None = None) -> bool:
    """Settle the connection's verification from a run that reached a terminal status.

    The connection's own verification run always settles it. Any other run on the
    same role settles it too while it is still pending or failed, so a person who
    fixes the cause and runs again sees the connection verified without reconnecting,
    while a later failure of an unrelated plan never takes a verified connection back.
    A cancel of a run that is not the connection's own proves nothing and is ignored.
    Returns whether the connection changed.
    """
    resolved = settings or get_settings()
    status = str(run.get("status", ""))
    if status in VERIFIED_RUN_STATUSES:
        outcome = "verified"
    elif status in FAILED_RUN_STATUSES:
        outcome = "failed"
    else:
        return False
    run_id = str(run.get("run_id", ""))
    role_arn = str(run.get("run_role_arn") or "")
    workspace_id = str(run.get("workspace_id", ""))
    if not run_id or not role_arn or not workspace_id:
        return False
    own = Attr(f"{CONNECTION_ATTRIBUTE}.run_id").eq(run_id)
    settles = own
    if status != "cancelled":
        settles = own | Attr(f"{CONNECTION_ATTRIBUTE}.verification").is_in(list(UNSETTLED_VERIFICATIONS))
    names = {"#connection": CONNECTION_ATTRIBUTE}
    values: dict[str, Any] = {":outcome": outcome, ":run": run_id, ":now": now_iso()}
    expression = "SET #connection.verification = :outcome, #connection.run_id = :run, #connection.verified_at = :now"
    if outcome == "failed":
        expression += ", #connection.verification_error = :error"
        values[":error"] = _verification_failure(run)
    else:
        expression += " REMOVE #connection.verification_error"
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression=expression,
            expression_names=names,
            expression_values=values,
            condition=Attr(f"{CONNECTION_ATTRIBUTE}.status").eq("connected")
            & Attr(f"{CONNECTION_ATTRIBUTE}.role_arn").eq(role_arn)
            & settles,
        )
    except ConditionFailed:
        return False
    return True


def current_connection(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """The workspace's recorded connection, or an empty mapping."""
    resolved = settings or get_settings()
    try:
        workspace = get_workspace(workspace_id, settings=resolved)
    except WorkspaceNotFound:
        return {}
    return dict(workspace.get(CONNECTION_ATTRIBUTE) or {})


def disconnect(workspace_id: str, *, stack_id: str, physical_id: str, settings: Settings | None = None) -> bool:
    """Forget the role a deleted stack created, if the workspace still uses it.

    Acts only when the workspace's connection names this stack and physical id, so
    deleting an old or unrelated stack changes nothing. The role is removed where it
    still stands, as the working role with its check outcome or as the staged one,
    and the connection reads `disconnected`. Returns whether anything changed.
    """
    resolved = settings or get_settings()
    try:
        workspace = get_workspace(workspace_id, settings=resolved)
    except WorkspaceNotFound:
        return False
    connection = dict(workspace.get(CONNECTION_ATTRIBUTE) or {})
    if connection.get("stack_id") != stack_id or connection.get("physical_id") != physical_id:
        return False
    role_arn = str(connection.get("role_arn") or "")
    removals: list[str] = []
    if role_arn and str(workspace.get("run_role_arn") or "") == role_arn:
        removals.extend(("run_role_arn", *CHECK_FIELDS))
    if role_arn and str(workspace.get("pending_run_role_arn") or "") == role_arn:
        removals.append("pending_run_role_arn")
    expression = "SET #connection.#status = :status, #connection.disconnected_at = :now, updated_at = :now"
    if removals:
        expression += " REMOVE " + ", ".join(removals)
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression=expression,
            expression_names={"#connection": CONNECTION_ATTRIBUTE, "#status": "status"},
            expression_values={":status": "disconnected", ":now": now_iso()},
            condition=Attr(f"{CONNECTION_ATTRIBUTE}.physical_id").eq(physical_id),
        )
    except ConditionFailed:
        return False
    return True


__all__ = [
    "CONNECTION_ATTRIBUTE",
    "FAILED_RUN_STATUSES",
    "PHYSICAL_ID_PREFIX",
    "TOKEN_EXPIRES_ATTRIBUTE",
    "TOKEN_HASH_ATTRIBUTE",
    "TOKEN_PATTERN",
    "TOKEN_TTL_SECONDS",
    "VERIFIED_RUN_STATUSES",
    "ConnectOutcome",
    "ConnectResult",
    "connect",
    "current_connection",
    "disconnect",
    "expected_role_arn",
    "fail_verification",
    "hash_token",
    "issue_token",
    "record_run",
    "record_verification",
    "run_role_name",
    "stack_account",
]
