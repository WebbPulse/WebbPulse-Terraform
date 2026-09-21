"""The task failure consumer: fails a run whose Fargate task never started.

`Plan` and `Apply` are `ecs:runTask.waitForTaskToken` states, so the execution sits
on a task token the runner container is supposed to send. When the task never runs
at all, because the image tag resolves to nothing, an ENI cannot be attached or the
pull is denied, there is no container to send it and the state waits out its six
hundred second heartbeat before the run errors. An EventBridge rule on ECS `Task
State Change` for the runner cluster delivers the stop here instead, and this sends
`SendTaskFailure` against the same token so the execution takes its existing
`MarkErrored` and `ReleaseSemaphoreAfterFailure` path within seconds.

The token needs no lookup table. Every phase state passes `RUN_ID` and `TASK_TOKEN`
as container environment overrides, and ECS echoes `overrides` back verbatim on the
state change event, so the event carries both the run it belongs to and the token to
fail. That is what keeps this path free of the write-then-read race the confirmations
queue has to live with.

Raising is how a record is retried, exactly as on the confirmations queue. Delivery
is at least once and the runner may have reported a result first, so the service side
treats a terminal run and an already consumed token as work already done rather than
as failures, and only a genuinely unresolvable delivery parks on the dead letter
queue.
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
"""The one ECS stop code this consumer acts on.

Every other stop code means the container ran, so the runner had its chance to report
and the state machine's own result path owns the outcome."""

TASK_FAILURE_ERROR = "TaskFailedToStart"
"""The `SendTaskFailure` error name, which is what the run's error records."""

RUN_ID_VARIABLE = "RUN_ID"
TASK_TOKEN_VARIABLE = "TASK_TOKEN"
"""The two container environment overrides every phase state sets, and the only
place the task token exists outside the execution."""

DEFAULT_STOPPED_REASON = "The Fargate task failed to start and ECS gave no reason."


class MalformedTaskFailure(Exception):
    """The message body is not a task stop this consumer can act on."""


class NotAFailedStart(Exception):
    """The stop is a real one this consumer must ignore rather than retry.

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


def parse_body(record: Mapping[str, Any]) -> tuple[str, str, str]:
    """The run id, task token and stopped reason one queue record carries.

    Raises:
        MalformedTaskFailure: The body is not a usable task stop. It still raises
            rather than returning, so the message parks on the dead letter queue
            instead of a run hanging on a token this consumer quietly dropped.
        NotAFailedStart: The task stopped for some reason other than failing to
            start, or carries neither of the two overrides, so the runner either
            ran or this is not a phase task at all.
    """
    detail = _detail(record)

    last_status = str(detail.get("lastStatus", ""))
    stop_code = str(detail.get("stopCode", ""))
    if last_status != "STOPPED" or stop_code != FAILED_TO_START_STOP_CODE:
        raise NotAFailedStart(f"The task is {last_status or 'statusless'} with stop code {stop_code or 'none'}.")

    overrides = _overrides(detail)
    run_id = overrides.get(RUN_ID_VARIABLE, "")
    task_token = overrides.get(TASK_TOKEN_VARIABLE, "")
    if not run_id or not task_token:
        raise NotAFailedStart("The task carries no run id and task token, so it is not a phase task.")

    reason = str(detail.get("stoppedReason", "") or "").strip() or DEFAULT_STOPPED_REASON
    return run_id, task_token, reason


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Fail one run whose phase task never started.

    A stop this consumer has no business acting on is logged and dropped rather than
    retried: retrying would park a perfectly ordinary task stop on the dead letter
    queue every time a run finishes normally.

    Raises:
        MalformedTaskFailure: The body is not a usable task stop.
        RunNotFound: No such run, so the delivery is retried in case it raced the
            run row rather than being dropped with a token still live.
    """
    try:
        run_id, task_token, reason = parse_body(record)
    except NotAFailedStart as error:
        _log.info(
            "Ignoring an ECS task stop that is not a phase task failing to start.",
            extra={"event": "runs.task_failure.ignored", "reason": str(error)},
        )
        return

    sent = service.fail_phase_task(
        run_id,
        task_token,
        error=TASK_FAILURE_ERROR,
        cause=reason,
        settings=settings,
    )
    _log.info(
        "Handled a phase task that failed to start.",
        extra={"event": "runs.task_failure.handled", "run_id": run_id, "sent": sent},
    )


__all__ = [
    "DEFAULT_STOPPED_REASON",
    "FAILED_TO_START_STOP_CODE",
    "RUN_ID_VARIABLE",
    "TASK_FAILURE_ERROR",
    "TASK_FAILURE_KIND",
    "TASK_TOKEN_VARIABLE",
    "MalformedTaskFailure",
    "NotAFailedStart",
    "handle_record",
    "parse_body",
]
