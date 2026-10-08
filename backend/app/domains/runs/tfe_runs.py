"""Runs, plans and applies rendered as HCP's `tfe.v2` resources, and their streamed logs.

The cloud backend drives a remote run by polling: it reads the run until it leaves
`pending`, streams the plan's log through go-tfe's `LogReader` until the plan reads
as done, then reads the run again to decide whether to ask for a confirmation. So a
plan here only reads as `finished` once its run can be confirmed or has moved on;
reading it finished while the confirmation token is still on its way would let the
CLI decide the run is not confirmable and exit.

Log URLs are what HCP calls archivist URLs: go-tfe fetches them with no
Authorization header and rewrites their query to `limit` and `offset` on every
read, so the credential is an HMAC in the path, valid for a whole phase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping
from typing import Any, Final

from ...common.composition.settings import Settings
from ...common.core import variable_cipher
from ...common.core.auth import RUNS_APPLY, RUNS_WRITE
from ...common.tfe.jsonapi import linkage, resource, timestamp
from . import service
from .schemas.run import Phase

STX: Final = b"\x02"
"""The byte go-tfe's log reader expects first, so it knows the stream supports an end marker."""

ETX: Final = b"\x03"
"""The byte that ends a log, sent only once its phase is over."""

LOG_URL_TTL_SECONDS: Final = 12 * 3600
"""How long a log URL reads. The CLI fetches it once and streams from it for the whole
phase, so it outlives the longest apply plus its wait in the queue."""

LOG_KEY_PURPOSE: Final = "tfe-log-read"
"""The HKDF purpose of the key that signs log URLs."""

APPLY_LOG_PREAMBLE: Final = "WebbPulse Terraform apply\nThe plan's version and platform are shown above.\n\n"
"""Three lines the cloud backend skips: it drops the first three lines of every apply log,
which on HCP repeat the engine's version banner."""

TERMINAL: Final = service.TERMINAL_STATUSES

RUN_STATUSES: Final[Mapping[str, str]] = {
    "pending": "pending",
    "planning": "planning",
    "applying": "applying",
    "applied": "applied",
    "planned_and_finished": "planned_and_finished",
    "errored": "errored",
    "cancelled": "canceled",
    "discarded": "discarded",
}
"""Stored run statuses under HCP's names. `planned` and `awaiting_confirmation` are
decided by `run_status`, since they depend on the confirmation token, and a saved plan
waiting for its `terraform apply` reads `planned_and_saved`."""

CANCELABLE: Final = frozenset({"pending", "planning", "planned", "applying"})
"""Statuses HCP offers a cancel in; a plan awaiting a decision is discarded instead."""


def plan_id(run_id: str) -> str:
    """The `plan-` id of a run's plan, which has no row of its own."""
    return "plan-" + run_id.removeprefix(service.RUN_ID_PREFIX)


def apply_id(run_id: str) -> str:
    """The `apply-` id of a run's apply."""
    return "apply-" + run_id.removeprefix(service.RUN_ID_PREFIX)


def run_id_of(phase_id: str) -> str:
    """The run a `plan-` or `apply-` id belongs to."""
    return service.RUN_ID_PREFIX + phase_id.split("-", 1)[1]


def _confirmable(run: Mapping[str, Any]) -> bool:
    """Whether a person can confirm the run now: it holds the state machine's task token."""
    return str(run.get("status", "")) in service.CONFIRMABLE_STATUSES and bool(run.get("confirm_task_token"))


def _confirmed(run: Mapping[str, Any]) -> bool:
    """Whether the run's plan was confirmed, by a person or by auto-apply."""
    decided = run.get("decision")
    return isinstance(decided, Mapping) and decided.get("action") == "confirmed"


def run_status(run: Mapping[str, Any]) -> str:
    """The run's HCP status. A plan waiting on its confirmation token still reads `planning`."""
    status = str(run.get("status", ""))
    if status in ("planned", "awaiting_confirmation"):
        if not _confirmable(run):
            return "planning"
        return "planned_and_saved" if run.get("save_plan") else "planned"
    return RUN_STATUSES.get(status, status)


def plan_status(run: Mapping[str, Any]) -> str:
    """The plan's HCP status: `running` until the run can be confirmed or has moved past its plan."""
    status = str(run.get("status", ""))
    if status == "pending":
        return "pending"
    if status == "planning" or (status in ("planned", "awaiting_confirmation") and not _confirmable(run)):
        return "running"
    if status in ("errored", "cancelled") and not _confirmed(run):
        return "errored" if status == "errored" else "canceled"
    return "finished"


def apply_status(run: Mapping[str, Any]) -> str:
    """The apply's HCP status, `unreachable` for a run that ended without one."""
    status = str(run.get("status", ""))
    if _confirmed(run):
        return {"applying": "running", "applied": "finished", "errored": "errored", "cancelled": "canceled"}.get(
            status, "running"
        )
    return "unreachable" if status in TERMINAL else "pending"


def phase_done(run: Mapping[str, Any], phase: Phase) -> bool:
    """Whether the phase's log is complete, which is when it gets its end marker."""
    current = plan_status(run) if phase == "plan" else apply_status(run)
    return current not in ("pending", "running")


def _counts(changes: Any) -> dict[str, int]:
    """The add, change and destroy counts of a stored `changes` block."""
    block = changes if isinstance(changes, Mapping) else {}
    return {field: int(block.get(field, 0) or 0) for field in ("add", "change", "destroy")}


def has_changes(run: Mapping[str, Any]) -> bool:
    """Whether the run's plan found anything to do."""
    return any(_counts(run.get("changes")).values())


def _phase_attributes(changes: Any, status: str, log_url: str) -> dict[str, Any]:
    """The attributes a plan and an apply share."""
    counts = _counts(changes)
    return {
        "log-read-url": log_url,
        "resource-additions": counts["add"],
        "resource-changes": counts["change"],
        "resource-destructions": counts["destroy"],
        "resource-imports": 0,
        "status": status,
    }


def plan_resource(run: Mapping[str, Any], log_url: str) -> dict[str, Any]:
    """The run's plan as HCP's `plans` resource."""
    attributes = _phase_attributes(run.get("changes"), plan_status(run), log_url)
    attributes["has-changes"] = has_changes(run)
    attributes["generated-configuration"] = False
    identifier = plan_id(str(run["run_id"]))
    return resource("plans", identifier, attributes, links={"self": f"/api/v2/plans/{identifier}"})


def apply_resource(run: Mapping[str, Any], log_url: str) -> dict[str, Any]:
    """The run's apply as HCP's `applies` resource, counting what the apply did."""
    attributes = _phase_attributes(run.get("apply_changes"), apply_status(run), log_url)
    identifier = apply_id(str(run["run_id"]))
    return resource("applies", identifier, attributes, links={"self": f"/api/v2/applies/{identifier}"})


def run_resource(run: Mapping[str, Any], held: Iterable[str]) -> dict[str, Any]:
    """One stored run as HCP's `runs` resource."""
    scopes = set(held)
    run_id = str(run["run_id"])
    status = str(run.get("status", ""))
    confirmable = _confirmable(run)
    attributes = {
        "actions": {
            "is-cancelable": status in CANCELABLE,
            "is-confirmable": confirmable,
            "is-discardable": confirmable,
            "is-force-cancelable": False,
        },
        "allow-empty-apply": False,
        "auto-apply": bool(run.get("auto_apply", False)),
        "created-at": timestamp(run.get("created_at")),
        "has-changes": has_changes(run),
        "is-destroy": bool(run.get("is_destroy", False)),
        "message": str(run.get("message") or ""),
        "permissions": {
            "can-apply": RUNS_APPLY in scopes,
            "can-cancel": RUNS_WRITE in scopes,
            "can-discard": RUNS_WRITE in scopes,
            "can-force-cancel": False,
            "can-force-execute": False,
        },
        "plan-only": bool(run.get("plan_only", False)),
        "position-in-queue": 0,
        "refresh": bool(run.get("refresh", True)),
        "refresh-only": bool(run.get("refresh_only", False)),
        "save-plan": bool(run.get("save_plan", False)),
        "replace-addrs": [str(address) for address in run.get("replace_addrs") or []],
        "source": "tfe-api",
        "status": run_status(run),
        "target-addrs": [str(address) for address in run.get("target_addrs") or []],
        "terraform-version": "",
        "trigger-reason": "manual",
        "variables": [{"key": str(key)} for key in run.get("run_variable_keys") or []],
    }
    relationships = {
        "apply": linkage("applies", apply_id(run_id)),
        "configuration-version": linkage("configuration-versions", str(run.get("config_version_id") or "") or None),
        "cost-estimate": linkage("cost-estimates", None),
        "plan": linkage("plans", plan_id(run_id)),
        "policy-checks": {"data": []},
        "run-events": {"data": []},
        "task-stages": {"data": []},
        "tf-policy-evaluations": {"data": []},
        "workspace": linkage("workspaces", str(run["workspace_id"])),
    }
    return resource("runs", run_id, attributes, relationships=relationships, links={"self": f"/api/v2/runs/{run_id}"})


def _mac(phase_identifier: str, expires: int, settings: Settings) -> str:
    """The URL safe HMAC binding one phase id to an expiry."""
    key = variable_cipher.derive_key(LOG_KEY_PURPOSE, settings)
    digest = hmac.new(key, f"{phase_identifier}:{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def log_token(phase_identifier: str, settings: Settings, *, now: float | None = None) -> str:
    """A path token reading one phase's log until it expires."""
    expires = int(now if now is not None else time.time()) + LOG_URL_TTL_SECONDS
    return f"{expires}.{_mac(phase_identifier, expires, settings)}"


def verify_log_token(phase_identifier: str, token: str, settings: Settings, *, now: float | None = None) -> bool:
    """Whether a path token reads this phase's log now."""
    expires_text, _, signature = token.partition(".")
    if not expires_text.isdigit() or not signature:
        return False
    expires = int(expires_text)
    if expires < int(now if now is not None else time.time()):
        return False
    try:
        expected = _mac(phase_identifier, expires, settings)
    except variable_cipher.MasterKeyUnavailable:
        return False
    return hmac.compare_digest(expected, signature)


def _cloudwatch_text(run_id: str, phase: Phase, settings: Settings) -> str:
    """The phase's log as the runner's transcript reads, rebuilt from its CloudWatch stream."""
    lines: list[str] = []
    after: str | None = None
    while True:
        page = service.run_logs(run_id, phase, after, limit=10_000, settings=settings)
        lines.extend("" if event["message"] == " " else str(event["message"]) for event in page["events"])
        following = page["next_after"]
        if not page["events"] or not following or following == after:
            break
        after = following
    return "\n".join(lines) + ("\n" if lines else "")


def _transcript(run_id: str, phase: Phase, settings: Settings) -> str | None:
    """The transcript the runner uploaded when the phase ended, or None."""
    if not settings.ARTIFACTS_BUCKET:
        return None
    from botocore.exceptions import ClientError

    try:
        response = service._s3(settings).get_object(  # pyright: ignore[reportPrivateUsage]
            Bucket=settings.ARTIFACTS_BUCKET, Key=service.log_key(run_id, phase)
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    return response["Body"].read().decode("utf-8", errors="replace")


def log_window(run: Mapping[str, Any], phase: Phase, offset: int, limit: int, settings: Settings) -> bytes:
    """`limit` bytes of the phase's log stream from `offset`, framed by STX and, once done, ETX.

    A running phase is read from CloudWatch, which the runner writes line by line. A
    finished one is read from its uploaded transcript, which is the same lines, so the
    offsets the CLI already holds stay valid across the switch.
    """
    run_id = str(run["run_id"])
    done = phase_done(run, phase)
    text = (_transcript(run_id, phase, settings) if done else None) or _cloudwatch_text(run_id, phase, settings)
    if phase == "apply" and (text or done):
        text = APPLY_LOG_PREAMBLE + text
    stream = STX + text.encode("utf-8") + (ETX if done else b"")
    return stream[max(0, offset) : max(0, offset) + max(0, limit)]
