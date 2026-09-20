"""Runs: creation, serialisation, the state transitions, logs and the runner bundle.

Two rules drive most of this module.

Runs against one workspace are serial. A create either starts a state machine
execution or, when that workspace already has a run in flight, stores the new run
`pending` with `queued_behind` set and starts nothing. The run ahead of it starts
it when it finishes, which is the only place a queued run is promoted.

A run token is minted when an execution starts and revoked when the run finishes.
It is a `wpk_` key whose subject is the run id and whose only scope is `runner`,
so it opens the two runner routes for one run and nothing else. Revoking it on
every terminal transition is what keeps a leaked token from outliving its run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Optional

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, Repository, new_ulid, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.core.auth import RUN_TOKEN_TENANT, RUNNER_SCOPE, api_key_store
from ...common.db import repositories
from ...common.db.tables import RUNS_BY_WORKSPACE_INDEX
from ..workspaces import service as workspaces_service
from . import session_policy
from .schemas.run import RUN_ROLE_DURATION_SECONDS, Phase

_log = logging.getLogger(__name__)

RUN_ID_PREFIX: Final = "run-"

EXECUTING_STATUSES: Final = frozenset({"planning", "planned", "awaiting_confirmation", "applying"})
"""A run in one of these has an execution and may hold the state lock. `planned` is
included: a run on its way to `awaiting_confirmation` has state locked."""

ACTIVE_STATUSES: Final = frozenset({"pending"}) | EXECUTING_STATUSES
"""A run in one of these occupies its workspace's slot, queued runs included, so a
new run queues behind the whole queue rather than racing its head."""

TERMINAL_STATUSES: Final = frozenset({"applied", "planned_and_finished", "errored", "cancelled", "discarded"})
"""A run in one of these is finished, so its token is dead and the next queued run
on its workspace may start."""

CONFIRMABLE_STATUSES: Final = frozenset({"awaiting_confirmation"})
"""Only a run holding a confirm task token can be confirmed."""

DISCARDABLE_STATUSES: Final = frozenset({"planned", "awaiting_confirmation"})
"""A discard drops a plan that was never applied. Earlier than this there is no
plan to drop and a cancel is the right verb; later the apply already ran."""

RUN_TOKEN_TTL: Final = timedelta(hours=4)
"""Just past the apply timeout of two hours plus the plan's thirty minutes, so a
token never expires underneath a run that is still legitimately working."""

ARTIFACT_URL_TTL: Final = 3600
"""One hour on the bundle's presigned URLs. The runner uses them immediately; the
apply phase re-fetches its bundle rather than reusing the plan phase's."""

PLAN_CONTENT_TYPE: Final = "application/octet-stream"
PLAN_JSON_CONTENT_TYPE: Final = "application/json"
MAX_PLAN_BYTES: Final = 500_000_000

LOG_CONTENT_TYPE: Final = "text/plain"
MAX_LOG_BYTES: Final = 50_000_000
"""A phase transcript is text, so fifty megabytes is far past any real run and
still small enough that a signed URL cannot be used to park a large object."""


class RunNotFound(Exception):
    """No run with that id."""


class ConfigVersionNotReady(Exception):
    """The config version exists but its tarball was never uploaded."""


class RunNotConfirmable(Exception):
    """The run is not waiting for a confirmation."""


class RunNotCancellable(Exception):
    """The run already finished, so there is nothing to stop."""


class RunNotDiscardable(Exception):
    """The run is not at a point where a plan can be dropped."""


class PhaseMismatch(Exception):
    """The reported phase is not the phase the run is in."""


def state_key(workspace_id: str) -> str:
    """The state object key for one workspace, which is why a rename is refused."""
    return f"workspaces/{workspace_id}/terraform.tfstate"


def plan_key(run_id: str) -> str:
    """The binary plan key for one run."""
    return f"runs/{run_id}/plan.tfplan"


def plan_json_key(run_id: str) -> str:
    """The JSON plan key for one run."""
    return f"runs/{run_id}/plan.json"


def log_key(run_id: str, phase: Phase) -> str:
    """The uploaded transcript key for one phase of one run."""
    return f"runs/{run_id}/{phase}.log"


def log_stream_name(run_id: str, phase: Phase) -> str:
    """The runner log stream for one phase of one run."""
    return f"{run_id}/{phase}"


def _runs(settings: Settings) -> Repository:
    """The runs table."""
    return repositories.runs(settings)


def _stepfunctions(settings: Settings) -> Any:
    """A Step Functions client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("stepfunctions", region_name=settings.AWS_REGION_NAME or None)


def _logs(settings: Settings) -> Any:
    """A CloudWatch Logs client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("logs", region_name=settings.AWS_REGION_NAME or None)


def active_run(
    workspace_id: str,
    *,
    statuses: frozenset[str] = ACTIVE_STATUSES,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    """The run currently holding `workspace_id`'s slot, if any.

    Reads newest first and returns the first matching row, so a workspace with a
    long history costs one page rather than a full partition scan.

    `statuses` defaults to every status that occupies the slot, which is what a new
    run asks about. Promotion passes `EXECUTING_STATUSES` instead, because the runs
    it is choosing between are themselves `pending`.
    """
    resolved = settings or get_settings()
    for item in _runs(resolved).iter_query(
        Key("workspace_id").eq(workspace_id),
        index_name=RUNS_BY_WORKSPACE_INDEX,
        ascending=False,
    ):
        if str(item.get("status", "")) in statuses:
            return item
    return None


def _queued_runs(workspace_id: str, *, settings: Settings) -> list[dict[str, Any]]:
    """The workspace's `pending` queued runs, oldest first, so the queue is FIFO."""
    items = [
        item
        for item in _runs(settings).iter_query(
            Key("workspace_id").eq(workspace_id),
            index_name=RUNS_BY_WORKSPACE_INDEX,
            ascending=True,
        )
        if str(item.get("status", "")) == "pending" and item.get("queued_behind")
    ]
    return items


def create_run(payload: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Create a run, starting it or queueing it behind the workspace's active one.

    Validates the workspace and the config version before writing anything, so a
    run never exists against a config version that was never uploaded.

    The config version is read with `persist` false: this function runs under the
    runs role, whose grant on the workspaces, variables and config-versions tables
    is read only by design, so the reconciliation against the bucket must not write
    the `uploaded` flip back. The workspaces domain owns that write and persists it
    on its own reads.

    Returns the stored run, carrying `run_token` only when an execution started.

    Raises:
        WorkspaceNotFound: No such workspace.
        RunRoleMissing: The workspace has no run role, so nothing could be assumed.
        ConfigVersionNotFound: No such config version on that workspace.
        ConfigVersionNotReady: The tarball was never uploaded.
    """
    resolved = settings or get_settings()
    workspace_id = str(payload["workspace_id"])
    config_version_id = str(payload["config_version_id"])

    workspace = workspaces_service.get_workspace(workspace_id, settings=resolved)
    if not str(workspace.get("run_role_arn", "") or ""):
        raise workspaces_service.RunRoleMissing(workspace_id)
    config_version = workspaces_service.get_config_version(
        workspace_id,
        config_version_id,
        persist=False,
        settings=resolved,
    )
    if str(config_version.get("status", "")) != "uploaded":
        raise ConfigVersionNotReady(config_version_id)

    blocking = active_run(workspace_id, settings=resolved)
    run_id = f"{RUN_ID_PREFIX}{new_ulid()}"
    timestamp = now_iso()
    item: dict[str, Any] = {
        "run_id": run_id,
        "workspace_id": workspace_id,
        "config_version_id": config_version_id,
        "status": "pending",
        "plan_only": bool(payload.get("plan_only", False)),
        "message": str(payload.get("message", "")),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if blocking is not None:
        item["queued_behind"] = str(blocking["run_id"])

    _runs(resolved).put(item, condition=Attr("run_id").not_exists())

    if blocking is not None:
        return item
    return start_run(run_id, settings=resolved)


def start_run(run_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Mint this run's token, start its execution and move it to `planning`.

    A run that already carries an `execution_arn` is returned untouched. Starting
    it again would mint a second token, overwrite the hash of the one the running
    execution is already carrying, and hit `ExecutionAlreadyExists` on the name,
    which is the run id. The caller gets no `run_token` back in that case, because
    the live plaintext only ever existed on the first start.

    The token is minted before the execution starts because it travels on the
    execution input: the state machine reads `$.run_token` into the plan and the
    apply container overrides, which is the only way the runner gets a
    `RUN_TOKEN`. The row is stamped before the start call so a crash between the
    two leaves a run that can be reconciled rather than a token with no run.

    The execution input is the one place the plaintext is written down, and the
    state machine runs with `include_execution_data` off so it never reaches the
    execution log. Only the hash is stored on the row.

    Returns the run with `run_token` set, which is the only time the plaintext
    reaches a caller.
    """
    from webbpulse.identity.api_keys import mint

    resolved = settings or get_settings()
    run = get_run(run_id, settings=resolved)
    if run.get("execution_arn"):
        return run

    minted = mint(
        user_id=run_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=(RUNNER_SCOPE,),
        name=f"run token {run_id}",
        expires_at=datetime.now(timezone.utc) + RUN_TOKEN_TTL,
        store=api_key_store(resolved),
    )

    execution_arn = ""
    if resolved.RUN_STATE_MACHINE_ARN:
        started = _stepfunctions(resolved).start_execution(
            stateMachineArn=resolved.RUN_STATE_MACHINE_ARN,
            name=run_id,
            input=json.dumps(
                {
                    "run_id": run_id,
                    "workspace_id": str(run["workspace_id"]),
                    "plan_only": bool(run.get("plan_only", False)),
                    "run_token": minted.plaintext,
                }
            ),
        )
        execution_arn = str(started.get("executionArn", ""))

    updated = _update_run(
        run_id,
        {
            "status": "planning",
            "started_at": now_iso(),
            "execution_arn": execution_arn,
            "run_token_hash": minted.record.key_hash,
            "queued_behind": None,
        },
        settings=resolved,
    )
    updated["run_token"] = minted.plaintext
    return updated


def _update_run(
    run_id: str,
    changes: dict[str, Any],
    *,
    settings: Settings,
    expected_statuses: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Apply `changes` to one run, optionally only from an expected status.

    A `None` value removes the attribute, which is how `queued_behind` clears when
    a queued run starts. The status guard makes every transition a conditional
    write, so two concurrent confirms cannot both advance the same run.

    The placeholders carry a `set_`/`:s` prefix because boto3 serialises the
    `ConditionExpression` into the same name and value maps as this
    `UpdateExpression`, numbering its own from `#n0` and `:v0`. A bare `:v0` here
    is silently overwritten by the condition's, which writes the guard's status
    string into whichever attribute sorted first.
    """
    sets: list[str] = []
    removes: list[str] = []
    values: dict[str, Any] = {}
    names: dict[str, str] = {}
    for index, (field, value) in enumerate(sorted(changes.items())):
        placeholder = f"#set_{index}"
        names[placeholder] = field
        if value is None:
            removes.append(placeholder)
            continue
        sets.append(f"{placeholder} = :s{index}")
        values[f":s{index}"] = value

    names["#set_updated"] = "updated_at"
    sets.append("#set_updated = :s_updated")
    values[":s_updated"] = now_iso()

    expression = "SET " + ", ".join(sets)
    if removes:
        expression += " REMOVE " + ", ".join(removes)

    condition: Any = Attr("run_id").exists()
    if expected_statuses is not None:
        ordered = sorted(expected_statuses)
        allowed: Any = Attr("status").eq(ordered[0])
        for status in ordered[1:]:
            allowed = allowed | Attr("status").eq(status)
        condition = condition & allowed

    try:
        result = _runs(settings).update(
            {"run_id": run_id},
            update_expression=expression,
            expression_values=values,
            expression_names=names,
            condition=condition,
            return_values="ALL_NEW",
        )
    except ConditionFailed as error:
        raise RunNotFound(run_id) from error
    return dict(result or {})


def get_run(run_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One run by id.

    Raises:
        RunNotFound: No such run.
    """
    resolved = settings or get_settings()
    item = _runs(resolved).get({"run_id": run_id})
    if not item:
        raise RunNotFound(run_id)
    return dict(item)


def list_runs(workspace_id: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """One workspace's runs, newest first.

    Raises:
        WorkspaceNotFound: No such workspace, which is a 404 rather than an empty list.
    """
    resolved = settings or get_settings()
    workspaces_service.get_workspace(workspace_id, settings=resolved)
    return [
        dict(item)
        for item in _runs(resolved).iter_query(
            Key("workspace_id").eq(workspace_id),
            index_name=RUNS_BY_WORKSPACE_INDEX,
            ascending=False,
        )
    ]


def confirm_run(run_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Confirm a planned run, releasing the state machine's confirmation wait.

    The status moves first, conditionally, and `SendTaskSuccess` follows. Doing it
    in that order means a duplicate confirm loses the conditional write and never
    reaches Step Functions, which would otherwise reject the second token anyway
    but only after the caller was told it succeeded.

    Raises:
        RunNotFound: No such run.
        RunNotConfirmable: The run is not awaiting a confirmation.
    """
    resolved = settings or get_settings()
    run = get_run(run_id, settings=resolved)
    if str(run.get("status", "")) not in CONFIRMABLE_STATUSES:
        raise RunNotConfirmable(run_id)
    token = str(run.get("confirm_task_token", ""))
    if not token:
        raise RunNotConfirmable(run_id)

    try:
        updated = _update_run(
            run_id,
            {"status": "applying", "confirm_task_token": None},
            settings=resolved,
            expected_statuses=frozenset(CONFIRMABLE_STATUSES),
        )
    except RunNotFound as error:
        raise RunNotConfirmable(run_id) from error

    _stepfunctions(resolved).send_task_success(
        taskToken=token,
        output=json.dumps({"run_id": run_id, "confirmed": True}),
    )
    return updated


def cancel_run(run_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Cancel a run, stopping its execution if it has one.

    A queued run has no execution, so cancelling it is a status write and a queue
    promotion. A running one is stopped and marked here rather than waiting for
    the execution's own terminal handler, so the caller sees the new state.

    Raises:
        RunNotFound: No such run.
        RunNotCancellable: The run already finished.
    """
    resolved = settings or get_settings()
    run = get_run(run_id, settings=resolved)
    status = str(run.get("status", ""))
    if status in TERMINAL_STATUSES:
        raise RunNotCancellable(run_id)

    execution_arn = str(run.get("execution_arn", ""))
    if execution_arn:
        _stepfunctions(resolved).stop_execution(
            executionArn=execution_arn,
            error="RunCancelled",
            cause=f"Cancelled through the API for {run_id}.",
        )

    return finish_run(run_id, "cancelled", settings=resolved)


def discard_run(run_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Discard a planned run, ending its execution through a task failure.

    A discard is not a cancel. The execution is waiting on the confirmation task
    token, so failing that token lets the state machine run its own terminal
    path and release the semaphore, which `StopExecution` would skip. That is why
    this sends `SendTaskFailure` rather than stopping the execution.

    Raises:
        RunNotFound: No such run.
        RunNotDiscardable: The run has no plan awaiting a decision.
    """
    resolved = settings or get_settings()
    run = get_run(run_id, settings=resolved)
    if str(run.get("status", "")) not in DISCARDABLE_STATUSES:
        raise RunNotDiscardable(run_id)

    token = str(run.get("confirm_task_token", ""))
    if token:
        _stepfunctions(resolved).send_task_failure(
            taskToken=token,
            error="RunDiscarded",
            cause=f"Discarded through the API for {run_id}.",
        )

    return finish_run(run_id, "discarded", settings=resolved)


def finish_run(
    run_id: str,
    status: str,
    *,
    error: str = "",
    changes: dict[str, int] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Move a run to a terminal status, revoke its token and promote its queue.

    The single exit for every ending, successful or not, so a token cannot outlive
    its run and a workspace cannot deadlock behind a run that errored.
    """
    resolved = settings or get_settings()
    updates: dict[str, Any] = {
        "status": status,
        "finished_at": now_iso(),
        "confirm_task_token": None,
        "queued_behind": None,
    }
    if error:
        updates["error"] = error
    if changes is not None:
        updates["changes"] = changes

    run = get_run(run_id, settings=resolved)
    updated = _update_run(run_id, updates, settings=resolved)
    _revoke_run_token(run, settings=resolved)
    _promote_queue(str(run["workspace_id"]), settings=resolved)
    return updated


def _revoke_run_token(run: dict[str, Any], *, settings: Settings) -> None:
    """Revoke this run's token, tolerating one that was already gone."""
    key_hash = str(run.get("run_token_hash", ""))
    if not key_hash:
        return
    api_key_store(settings).revoke(key_hash)


def _promote_queue(workspace_id: str, *, settings: Settings) -> None:
    """Start the oldest run queued on this workspace, if nothing else is active.

    Best effort: a failure to start the next run must not fail the transition that
    finished the previous one, which would leave a run stuck non-terminal. It is
    logged rather than swallowed silently, because a promotion that never happens
    leaves a run `pending` with nothing left to start it.
    """
    if active_run(workspace_id, statuses=EXECUTING_STATUSES, settings=settings) is not None:
        return
    queued = _queued_runs(workspace_id, settings=settings)
    if not queued:
        return
    next_run_id = str(queued[0]["run_id"])
    try:
        start_run(next_run_id, settings=settings)
    except Exception as error:  # noqa: BLE001
        _log.exception(
            "Could not promote the next queued run.",
            extra={
                "event": "runs.promote.failed",
                "run_id": next_run_id,
                "workspace_id": workspace_id,
                "error": type(error).__name__,
            },
        )


def record_phase_result(
    run_id: str,
    result: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Record a phase's outcome and move the run to its next status.

    The transitions, all of them:

    A failed plan or apply errors the run. A successful apply applies it. A
    successful plan finishes the run when it was `plan_only` or found no changes,
    and otherwise leaves it `awaiting_confirmation` for a human.

    Raises:
        RunNotFound: No such run.
        PhaseMismatch: The reported phase is not the one the run is in.
    """
    resolved = settings or get_settings()
    run = get_run(run_id, settings=resolved)
    status = str(run.get("status", ""))
    phase = str(result["phase"])
    expected = {"plan": "planning", "apply": "applying"}[phase]
    if status != expected:
        raise PhaseMismatch(f"{run_id} is {status}, not {expected}")

    changes = dict(result.get("changes") or {"add": 0, "change": 0, "destroy": 0})
    exit_code = int(result.get("exit_code", 0))
    error = str(result.get("error", ""))

    if exit_code != 0:
        return finish_run(
            run_id,
            "errored",
            error=error or f"The {phase} phase exited {exit_code}.",
            changes=changes,
            settings=resolved,
        )

    if phase == "apply":
        return finish_run(run_id, "applied", changes=changes, settings=resolved)

    has_changes = any(int(changes.get(field, 0)) for field in ("add", "change", "destroy"))
    if bool(run.get("plan_only", False)) or not has_changes:
        return finish_run(run_id, "planned_and_finished", changes=changes, settings=resolved)

    return _update_run(
        run_id,
        {"status": "awaiting_confirmation", "changes": changes},
        settings=resolved,
        expected_statuses=frozenset({"planning"}),
    )


def store_confirm_task_token(
    run_id: str,
    task_token: str,
    *,
    expected_statuses: frozenset[str] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store the confirmation task token the state machine is waiting on.

    Reached through the confirmations queue rather than by a person: the state
    machine sends the token to SQS and the consumer lands it here.

    Args:
        run_id: The run the token belongs to.
        task_token: The Step Functions task token a confirm resumes.
        expected_statuses: Statuses the write is conditional on, so a token cannot
            land on a run that already finished. `None` writes unconditionally.
        settings: Settings override, for the suite.

    Raises:
        RunNotFound: No such run, or it is not in `expected_statuses`.
    """
    resolved = settings or get_settings()
    return _update_run(
        run_id,
        {"confirm_task_token": task_token},
        settings=resolved,
        expected_statuses=expected_statuses,
    )


def run_logs(
    run_id: str,
    phase: Phase,
    after: Optional[str] = None,
    *,
    limit: int = 500,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One page of a run phase's logs, plus the token that continues it.

    An absent stream is an empty page with a `None` token rather than a 404: a
    caller polling a run that has not started its task yet should keep polling.

    Raises:
        RunNotFound: No such run.
    """
    resolved = settings or get_settings()
    get_run(run_id, settings=resolved)
    if not resolved.RUNNER_LOG_GROUP:
        return {"run_id": run_id, "phase": phase, "events": [], "next_after": None}

    from botocore.exceptions import ClientError

    kwargs: dict[str, Any] = {
        "logGroupName": resolved.RUNNER_LOG_GROUP,
        "logStreamName": log_stream_name(run_id, phase),
        "limit": limit,
        "startFromHead": True,
    }
    if after:
        kwargs["nextToken"] = after

    try:
        response = _logs(resolved).get_log_events(**kwargs)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {"run_id": run_id, "phase": phase, "events": [], "next_after": None}
        raise

    events = [
        {"timestamp": int(event.get("timestamp", 0)), "message": str(event.get("message", ""))}
        for event in response.get("events", [])
    ]
    return {
        "run_id": run_id,
        "phase": phase,
        "events": events,
        "next_after": response.get("nextForwardToken") or None,
    }


def _phase_for_status(status: str) -> Phase:
    """Which phase a run in `status` is executing."""
    return "apply" if status == "applying" else "plan"


def run_bundle(run_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Everything the runner needs for this run's current phase.

    The phase is derived from the run's status rather than taken from the caller,
    so a runner holding a plan-phase token cannot ask for the apply phase's
    unrestricted session policy.

    Raises:
        RunNotFound: No such run.
        WorkspaceNotFound: The workspace was deleted under the run.
        ConfigVersionNotFound: The config version was deleted under the run.
    """
    from webbpulse.storage import presigned_get

    resolved = settings or get_settings()
    run = get_run(run_id, settings=resolved)
    workspace_id = str(run["workspace_id"])
    workspace = workspaces_service.get_workspace(workspace_id, settings=resolved)
    config_version = workspaces_service.get_config_version(
        workspace_id, str(run["config_version_id"]), settings=resolved
    )

    phase = _phase_for_status(str(run.get("status", "")))
    variables = workspaces_service.resolved_variables(workspace_id, settings=resolved)
    region = resolved.AWS_REGION_NAME
    endpoint = resolved.s3_endpoint_url
    workspace_state_key = state_key(workspace_id)

    return {
        "run_id": run_id,
        "workspace_id": workspace_id,
        "phase": phase,
        "plan_only": bool(run.get("plan_only", False)),
        "engine": str(workspace.get("engine", "terraform")),
        "engine_version": str(workspace.get("engine_version", "")),
        "working_directory": str(workspace.get("working_directory", "")),
        "config_url": presigned_get(
            resolved.ARTIFACTS_BUCKET,
            str(config_version["key"]),
            ARTIFACT_URL_TTL,
            region_name=region,
            endpoint_url=endpoint,
        ).url,
        "backend": {
            "bucket": resolved.STATE_BUCKET,
            "key": workspace_state_key,
            "region": region,
            "kms_key_id": resolved.STATE_KMS_KEY_ARN,
        },
        "run_role": {
            "role_arn": str(workspace.get("run_role_arn", "")),
            "external_id": workspace_id,
            "session_policy": session_policy.for_phase(
                phase,
                state_bucket=resolved.STATE_BUCKET,
                state_key=workspace_state_key,
                artifacts_bucket=resolved.ARTIFACTS_BUCKET,
                run_id=run_id,
            ),
            "duration_seconds": RUN_ROLE_DURATION_SECONDS,
        },
        "terraform_variables": variables["terraform"],
        "environment_variables": variables["env"],
        "artifacts": _artifacts(run_id, phase, settings=resolved),
    }


def _artifacts(run_id: str, phase: Phase, *, settings: Settings) -> dict[str, str]:
    """Presigned URLs for one run's artifacts, both directions.

    The log URL is per phase because a plan and an apply each upload their own
    redacted transcript and neither should overwrite the other.
    """
    from webbpulse.storage import presigned_get, presigned_put

    region = settings.AWS_REGION_NAME
    endpoint = settings.s3_endpoint_url
    bucket = settings.ARTIFACTS_BUCKET
    return {
        "plan_put_url": presigned_put(
            bucket,
            plan_key(run_id),
            PLAN_CONTENT_TYPE,
            MAX_PLAN_BYTES,
            ARTIFACT_URL_TTL,
            region_name=region,
            endpoint_url=endpoint,
        ).url,
        "plan_json_put_url": presigned_put(
            bucket,
            plan_json_key(run_id),
            PLAN_JSON_CONTENT_TYPE,
            MAX_PLAN_BYTES,
            ARTIFACT_URL_TTL,
            region_name=region,
            endpoint_url=endpoint,
        ).url,
        "plan_get_url": presigned_get(
            bucket,
            plan_key(run_id),
            ARTIFACT_URL_TTL,
            region_name=region,
            endpoint_url=endpoint,
        ).url,
        "log_put_url": presigned_put(
            bucket,
            log_key(run_id, phase),
            LOG_CONTENT_TYPE,
            MAX_LOG_BYTES,
            ARTIFACT_URL_TTL,
            region_name=region,
            endpoint_url=endpoint,
        ).url,
    }


def render_run(item: dict[str, Any]) -> dict[str, Any]:
    """Strip the stored-only fields a run row carries.

    The task tokens and the token hash never leave the service: a caller holding a
    confirm task token could confirm a run it has no scope for.
    """
    hidden = {"confirm_task_token", "run_token_hash"}
    return {field: value for field, value in item.items() if field not in hidden}


__all__ = [
    "ACTIVE_STATUSES",
    "ARTIFACT_URL_TTL",
    "CONFIRMABLE_STATUSES",
    "DISCARDABLE_STATUSES",
    "ConfigVersionNotReady",
    "PhaseMismatch",
    "RUN_ID_PREFIX",
    "RUN_TOKEN_TTL",
    "RunNotCancellable",
    "RunNotConfirmable",
    "RunNotDiscardable",
    "RunNotFound",
    "TERMINAL_STATUSES",
    "active_run",
    "cancel_run",
    "confirm_run",
    "create_run",
    "discard_run",
    "finish_run",
    "get_run",
    "list_runs",
    "log_key",
    "log_stream_name",
    "plan_json_key",
    "plan_key",
    "record_phase_result",
    "render_run",
    "run_bundle",
    "run_logs",
    "start_run",
    "state_key",
    "store_confirm_task_token",
]
