"""Whether the runner can assume a workspace's run role, answered from the runner itself.

The run role trusts the runner's plan and apply task roles and nothing else, so no
principal the API holds can assume it, and none should: a principal that can assume
the role holds whatever the role grants, whatever session policy it promises to pass.
The check therefore never calls STS. It reads the outcome the runner itself recorded
the last time it tried the role: every run stamps the role ARN it was created with,
and a run either got past the runner's AssumeRole, failed on it, or ended before it.

The answer is one of three:

- `connected`: the newest run with a verdict got past AssumeRole on this ARN.
- `failed`: the newest run with a verdict failed on AssumeRole for this ARN.
- `unverified`: no run with a verdict has used this ARN yet, so a plan only run is
  the check.
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.db.tables import RUNS_BY_WORKSPACE_INDEX, SEMAPHORE_RUN_ID
from ...common.workspaces.reads import RunRoleMissing, get_workspace

RunRoleCheckStatus = Literal["connected", "failed", "unverified"]
"""What the runner's record says about one run role."""

ASSUMED_STATUSES: Final = frozenset(
    {"planned", "awaiting_confirmation", "applying", "applied", "planned_and_finished", "discarded"}
)
"""Run statuses only reachable after the plan phase assumed the role."""

ASSUMED_FAILURES: Final = frozenset({"InitFailed", "PlanFailed", "PlanShowFailed", "ApplyFailed"})
"""Runner failures raised by the engine, which only runs once the role is assumed."""

ASSUME_ROLE_FAILURE: Final = "AssumeRoleFailed"
"""The runner failure raised when AssumeRole on the run role is refused."""

EVIDENCE_SCAN_LIMIT: Final = 50
"""How many of a workspace's newest runs are read looking for a verdict."""

RUN_ROLE_ASSUME_FAILED_MESSAGE: Final = (
    "The runner could not assume the role. Its trust policy has to name every runner task role "
    "with the workspace id as the external id, and its name has to keep the suggested prefix."
)
"""Why a role the runner could not assume failed, since STS will not say which part."""

RUN_ROLE_UNVERIFIED_MESSAGE: Final = (
    "No run has assumed this role yet. Start a plan only run: the runner assumes the role at the "
    "start of every phase, and the outcome shows here."
)
"""What a role no run has tried yet reads as."""

_FAILURE_NAME = re.compile(r"failed with (\w+)")
_ENGINE_EXIT = re.compile(r"^The (plan|apply) phase exited")
_ACCOUNT_ID = re.compile(r"^arn:aws[\w-]*:iam::(\d{12}):role/")


def _failure_name(error: str) -> str | None:
    """The runner failure name inside a run's stored error, or `None`."""
    match = _FAILURE_NAME.search(error)
    return match.group(1) if match else None


def _account_id(role_arn: str) -> str | None:
    """The account a role ARN names, or `None` for an ARN of another shape."""
    match = _ACCOUNT_ID.match(role_arn)
    return match.group(1) if match else None


def _verdict(run: dict[str, Any]) -> RunRoleCheckStatus | None:
    """What one run proves about its role, or `None` when it ended before AssumeRole."""
    if str(run.get("status", "")) in ASSUMED_STATUSES:
        return "connected"
    error = str(run.get("error", "") or "")
    if _ENGINE_EXIT.match(error):
        return "connected"
    name = _failure_name(error)
    if name == ASSUME_ROLE_FAILURE:
        return "failed"
    if name in ASSUMED_FAILURES:
        return "connected"
    return None


def _evidence(workspace_id: str, role_arn: str, settings: Settings) -> tuple[dict[str, Any], RunRoleCheckStatus] | None:
    """The newest run that tried `role_arn` and reached a verdict, with that verdict."""
    scanned = 0
    for item in repositories.runs(settings).iter_query(
        Key("workspace_id").eq(workspace_id),
        index_name=RUNS_BY_WORKSPACE_INDEX,
        ascending=False,
    ):
        if str(item.get("run_id", "")) == SEMAPHORE_RUN_ID:
            continue
        scanned += 1
        if str(item.get("run_role_arn", "") or "") == role_arn:
            verdict = _verdict(item)
            if verdict is not None:
                return item, verdict
        if scanned >= EVIDENCE_SCAN_LIMIT:
            break
    return None


def probe_run_role(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Report whether the runner has assumed the workspace's run role, writing nothing.

    Reads the workspace's newest runs and answers from the first one created with
    the current role ARN that reached AssumeRole. No credentials are requested, so
    the API holds no path into the account the role lives in.

    Raises:
        WorkspaceNotFound: No such workspace.
        RunRoleMissing: The workspace carries no run role ARN.
    """
    resolved = settings or get_settings()
    workspace = get_workspace(workspace_id, settings=resolved)
    role_arn = str(workspace.get("run_role_arn", "") or "")
    if not role_arn:
        raise RunRoleMissing(workspace_id)

    found = _evidence(workspace_id, role_arn, resolved)
    if found is None:
        return {
            "connected": False,
            "status": "unverified",
            "account_id": None,
            "error": RUN_ROLE_UNVERIFIED_MESSAGE,
            "run_id": None,
            "checked_at": None,
        }
    run, verdict = found
    checked_at = str(run.get("finished_at") or run.get("updated_at") or run.get("created_at") or "") or None
    if verdict == "failed":
        return {
            "connected": False,
            "status": "failed",
            "account_id": None,
            "error": RUN_ROLE_ASSUME_FAILED_MESSAGE,
            "run_id": str(run["run_id"]),
            "checked_at": checked_at,
        }
    return {
        "connected": True,
        "status": "connected",
        "account_id": _account_id(role_arn),
        "error": None,
        "run_id": str(run["run_id"]),
        "checked_at": checked_at,
    }


def check_run_role(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Probe the workspace's run role and stamp the outcome on the row.

    The probe itself is `probe_run_role`. What this adds is the record the UI reads
    between visits: the moment and the account on a success, both cleared
    otherwise, so a stale success cannot outlive a broken trust policy.

    Raises:
        WorkspaceNotFound: No such workspace.
        RunRoleMissing: The workspace carries no run role ARN.
    """
    resolved = settings or get_settings()
    outcome = probe_run_role(workspace_id, settings=resolved)
    _record(workspace_id, outcome, settings=resolved)
    return outcome


def _record(workspace_id: str, outcome: dict[str, Any], *, settings: Settings) -> None:
    """Stamp or clear the run role check fields on one workspace row."""
    repository = repositories.workspaces(settings)
    account_id = outcome["account_id"] if outcome["connected"] else None
    if account_id:
        repository.update(
            {"workspace_id": workspace_id},
            update_expression="SET run_role_checked_at = :checked, run_role_account_id = :account",
            expression_values={":checked": outcome["checked_at"] or now_iso(), ":account": account_id},
            condition=Attr("workspace_id").exists(),
        )
        return
    repository.update(
        {"workspace_id": workspace_id},
        update_expression="REMOVE run_role_checked_at, run_role_account_id",
        condition=Attr("workspace_id").exists(),
    )
