"""Resolving a phase's Step Functions task token on the runner's behalf.

`SendTaskSuccess` and `SendTaskFailure` accept no resource scoping for a callback
token, so a runner task allowed to call them could complete any execution's
waiting state. The runner therefore holds no Step Functions permission. It
reports through `POST /runs/{id}/phase-result` with its run token, and this module
resolves the token its phase waits on from that task's own overrides and sends
the outcome. A runner that dies before it can report is failed by the task stop
consumer instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from ...common.composition.settings import Settings, get_settings
from . import runner_tokens, service

_log = logging.getLogger(__name__)


def _stepfunctions(settings: Settings) -> Any:
    """A Step Functions client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("stepfunctions", region_name=settings.AWS_REGION_NAME or None)


class PhaseTaskUnresolved(Exception):
    """No live runner task of this run's phase could be found to resolve."""


def phase_token(run: Mapping[str, Any], phase: str, *, settings: Settings) -> str:
    """The task token this run's phase waits on.

    Raises:
        PhaseTaskUnresolved: No runner task exchanged for the run, or it has stopped
            or was started for another run or phase.
    """
    try:
        return runner_tokens.phase_task_token(run, phase, settings=settings)
    except runner_tokens.ExchangeRefused as error:
        raise PhaseTaskUnresolved(str(error)) from error


def succeed(task_token: str, exit_code: int, changes: Mapping[str, Any], *, settings: Settings) -> None:
    """Hand the state machine the phase's exit code and change counts."""
    payload = {
        "exit_code": exit_code,
        "changes": {field: int(changes.get(field, 0) or 0) for field in ("add", "change", "destroy")},
    }
    _stepfunctions(settings).send_task_success(taskToken=task_token, output=json.dumps(payload))


def fail(run_id: str, task_token: str, error: str, cause: str, *, settings: Settings) -> bool:
    """Hand the state machine the runner's error name, returning whether it was sent.

    A token already consumed means the execution moved on, which is work done.
    """
    from botocore.exceptions import ClientError

    try:
        _stepfunctions(settings).send_task_failure(
            taskToken=task_token,
            error=error[: service.ERROR_MAX_LENGTH],
            cause=cause[: service.CAUSE_MAX_LENGTH],
        )
    except ClientError as client_error:
        code = str(client_error.response.get("Error", {}).get("Code", ""))
        if code in service.CONSUMED_TOKEN_ERRORS:
            return False
        raise
    _log.info(
        "Failed a phase on the runner's report.",
        extra={"event": "runs.phase_task.reported_failure", "run_id": run_id, "error": error},
    )
    return True


REPORTED_FAILURE_ERROR = "PhaseFailed"
"""The error name for a phase result that carries a failing exit code but no name."""


def report(run_id: str, result: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Record a phase's outcome and resolve the task token its state waits on.

    A result naming an `error_name` is a runner failure: only the token is failed,
    and the state machine's own error path marks the run, so the run's error reads
    `The run failed with <error_name>.` exactly as the run role check expects. Any
    other result is recorded as before and then sent as the task's success, or as
    `PhaseFailed` when its exit code says the phase failed.

    The token is resolved before anything is written, so a report from a task that
    cannot be matched to the phase changes nothing.

    Raises:
        RunNotFound: No such run.
        PhaseMismatch: The reported phase is not the one the run is in.
        PhaseTaskUnresolved: The phase's runner task cannot be resolved.
    """
    resolved = settings or get_settings()
    run = service.get_run(run_id, settings=resolved)
    phase = str(result["phase"])
    expected = {"plan": "planning", "apply": "applying"}[phase]
    if run.get("status") != expected:
        raise service.PhaseMismatch(f"{run_id} is {run.get('status')}, not {expected}")
    token = phase_token(run, phase, settings=resolved)

    error_name = str(result.get("error_name") or "")
    if error_name:
        fail(run_id, token, error_name, str(result.get("error") or ""), settings=resolved)
        return run

    updated = service.record_phase_result(run_id, result, settings=resolved)
    exit_code = int(result.get("exit_code", 0))
    if exit_code != 0 and not (phase == "plan" and exit_code == service.PLAN_CHANGES_EXIT):
        fail(run_id, token, REPORTED_FAILURE_ERROR, str(result.get("error") or ""), settings=resolved)
    else:
        succeed(token, exit_code, dict(result.get("changes") or {}), settings=resolved)
    return updated


__all__ = ["REPORTED_FAILURE_ERROR", "PhaseTaskUnresolved", "fail", "phase_token", "report", "succeed"]
