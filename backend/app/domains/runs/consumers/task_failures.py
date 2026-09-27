"""The task stop consumer: fails a phase whose Fargate task stopped without reporting.

`Plan` and `Apply` are `ecs:runTask.waitForTaskToken` states, so the execution sits
on a task token. The runner holds no Step Functions permission and reports through
the phase result route instead, so a task that never starts (an image tag that
resolves to nothing, an ENI that cannot attach, a denied pull) or a runner that
dies before it can report leaves nobody to resolve that token until the state's
heartbeat expires. An EventBridge rule on ECS `Task State Change` for every stopped
runner task delivers the stop here, and this sends `SendTaskFailure` against the
same token so the execution takes its existing `MarkErrored` and
`ReleaseSemaphoreAfterFailure` path within seconds.

The token needs no lookup table. Every phase state passes `RUN_ID`, `PHASE` and
`TASK_TOKEN` as container environment overrides, and ECS echoes `overrides` back
verbatim on the state change event. Most stops are of a runner that already
reported, whose token Step Functions has consumed, so a run that has left the
stopped task's phase is left alone and a consumed token is work already done.

Raising is how a record is retried, exactly as on the confirmations queue, and only
a genuinely unresolvable delivery parks on the dead letter queue.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from ....common.composition.settings import Settings
from .. import service

_log = logging.getLogger(__name__)

TASK_FAILURE_KIND = "run_task_failed_to_start"
"""The `kind` the EventBridge rule's input transformer stamps on the message.

An ECS state change carries no kind of its own, so the rule adds one and this queue
can hold both message shapes without either consumer guessing at the other's."""

FAILED_TO_START_STOP_CODE = "TaskFailedToStart"
"""The ECS stop code of a task whose container never ran."""

TASK_FAILURE_ERROR = "TaskFailedToStart"
"""The `SendTaskFailure` error name for a task that never started."""

RUNNER_STOPPED_ERROR = "RunnerStopped"
"""The `SendTaskFailure` error name for a runner that ran but stopped unreported."""

RUN_ID_VARIABLE = "RUN_ID"
PHASE_VARIABLE = "PHASE"
TASK_TOKEN_VARIABLE = "TASK_TOKEN"
"""The container environment overrides every phase state sets, the last the only
place the task token exists outside the execution."""

PHASE_STATUSES = {"plan": "planning", "apply": "applying"}
"""The run status a phase task's run holds while that phase is still unresolved."""

DEFAULT_STOPPED_REASON = "The Fargate task stopped and ECS gave no reason."


class MalformedTaskFailure(Exception):
    """The message body is not a task stop this consumer can act on."""


class NotAPhaseStop(Exception):
    """The stop is one this consumer must ignore rather than retry.

    Separate from `MalformedTaskFailure` because it is not a fault: the message was
    well formed and the answer is that there is nothing to do.
    """


def _detail(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ECS task detail one queue record carries.

    Raises:
        MalformedTaskFailure: The body is not JSON, is not an object, is not a
            `run_task_failed_to_start`, or carries no detail object.
    """
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise MalformedTaskFailure("The record carries no body.")

    try:
        body = json.loads(raw)
    except ValueError as error:
        raise MalformedTaskFailure("The body is not JSON.") from error

    if not isinstance(body, Mapping):
        raise MalformedTaskFailure("The body is not a JSON object.")

    kind = str(body.get("kind", ""))
    if kind != TASK_FAILURE_KIND:
        raise MalformedTaskFailure(f"The body is a {kind or 'kindless'} message, not a {TASK_FAILURE_KIND}.")

    detail = body.get("detail")
    if not isinstance(detail, Mapping):
        raise MalformedTaskFailure("The body carries no ECS task detail.")
    return detail


def _overrides(detail: Mapping[str, Any]) -> dict[str, str]:
    """Every container environment override on the task, flattened to one mapping.

    A phase task has exactly one container, and the names this consumer wants are
    the ones the state machine sets, so flattening across containers loses nothing
    and keeps the parse indifferent to the container's name.
    """
    overrides = detail.get("overrides")
    if not isinstance(overrides, Mapping):
        return {}

    container_overrides = overrides.get("containerOverrides")
    if not isinstance(container_overrides, list):
        return {}

    flattened: dict[str, str] = {}
    for container in container_overrides:
        if not isinstance(container, Mapping):
            continue
        environment = container.get("environment")
        if not isinstance(environment, list):
            continue
        for entry in environment:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            value = entry.get("value")
            if isinstance(name, str) and isinstance(value, str):
                flattened[name] = value
    return flattened


def _stop_cause(detail: Mapping[str, Any]) -> str:
    """The stopped reason with each container's exit code, which carry no secrets."""
    reason = str(detail.get("stoppedReason", "") or "").strip() or DEFAULT_STOPPED_REASON
    containers = detail.get("containers")
    codes = (
        [
            f"{container.get('name', 'container')} exited {container['exitCode']}"
            for container in containers or []
            if isinstance(container, Mapping) and isinstance(container.get("exitCode"), int)
        ]
        if isinstance(containers, list)
        else []
    )
    return "; ".join([reason, *codes])


def parse_body(record: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """The run id, phase, task token, error name and cause one queue record carries.

    Raises:
        MalformedTaskFailure: The body is not a usable task stop. It still raises
            rather than returning, so the message parks on the dead letter queue
            instead of a run hanging on a token this consumer quietly dropped.
        NotAPhaseStop: The task has not stopped, or carries no run id, phase and
            task token, so it is not a phase task at all.
    """
    detail = _detail(record)

    last_status = str(detail.get("lastStatus", ""))
    if last_status != "STOPPED":
        raise NotAPhaseStop(f"The task is {last_status or 'statusless'}, not STOPPED.")

    overrides = _overrides(detail)
    run_id = overrides.get(RUN_ID_VARIABLE, "")
    phase = overrides.get(PHASE_VARIABLE, "")
    task_token = overrides.get(TASK_TOKEN_VARIABLE, "")
    if not run_id or not task_token or phase not in PHASE_STATUSES:
        raise NotAPhaseStop("The task carries no run id, phase and task token, so it is not a phase task.")

    stop_code = str(detail.get("stopCode", ""))
    error = TASK_FAILURE_ERROR if stop_code == FAILED_TO_START_STOP_CODE else RUNNER_STOPPED_ERROR
    return run_id, phase, task_token, error, _stop_cause(detail)


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Fail one run whose phase task stopped while its run was still in that phase.

    A stop this consumer has no business acting on is logged and dropped rather than
    retried: retrying would park a perfectly ordinary task stop on the dead letter
    queue every time a run finishes normally.

    Raises:
        MalformedTaskFailure: The body is not a usable task stop.
        RunNotFound: No such run, so the delivery is retried in case it raced the
            run row rather than being dropped with a token still live.
    """
    try:
        run_id, phase, task_token, error, cause = parse_body(record)
    except NotAPhaseStop as reason:
        _log.info(
            "Ignoring an ECS task stop that is not a phase task.",
            extra={"event": "runs.task_failure.ignored", "reason": str(reason)},
        )
        return

    sent = service.fail_phase_task(
        run_id,
        task_token,
        error=error,
        cause=cause,
        expected_status=PHASE_STATUSES[phase],
        settings=settings,
    )
    _log.info(
        "Handled a stopped phase task.",
        extra={"event": "runs.task_failure.handled", "run_id": run_id, "error": error, "sent": sent},
    )


__all__ = [
    "DEFAULT_STOPPED_REASON",
    "FAILED_TO_START_STOP_CODE",
    "PHASE_STATUSES",
    "PHASE_VARIABLE",
    "RUNNER_STOPPED_ERROR",
    "RUN_ID_VARIABLE",
    "TASK_FAILURE_ERROR",
    "TASK_FAILURE_KIND",
    "TASK_TOKEN_VARIABLE",
    "MalformedTaskFailure",
    "NotAPhaseStop",
    "handle_record",
    "parse_body",
]
