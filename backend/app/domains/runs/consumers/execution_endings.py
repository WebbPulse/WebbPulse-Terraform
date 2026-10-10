"""The execution endings consumer: finishes a run whose execution ended without doing so.

Every failing path of the run state machine ends in a `Mark*` state that writes the
run's terminal status, retried on throttling and other transient faults. A write
that still fails, an execution that times out, or one stopped from the console ends
the execution with the run left in a live status, holding its workspace queue and a
semaphore slot until someone notices. An EventBridge rule on Step Functions
`Execution Status Change` for the run state machine delivers every `FAILED`,
`TIMED_OUT` and `ABORTED` ending here, stamped with this kind by the rule's input
transformer, and this finishes the run through `finish_run` and drops its slot.

The execution name is the run id, so no lookup is needed. Most deliveries are of a
run the state machine or the API already finished, and those are left alone.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final, Mapping

from ....common.composition.settings import Settings
from .. import service

_log = logging.getLogger(__name__)

KIND: Final = "run_execution_ended"
"""The `kind` the EventBridge rule's input transformer stamps on the message."""

ENDED_STATUSES: Final = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})
"""The execution statuses that can leave a run unfinished."""

CANCELLED_ERROR: Final = "RunCancelled"
"""The error name an execution stopped by the API's cancel carries."""


class MalformedExecutionEnding(Exception):
    """The message body is not an execution status change this consumer can act on."""


def parse_body(record: Mapping[str, Any]) -> tuple[str, str, str]:
    """The run id, execution status and error name one queue record carries.

    Raises:
        MalformedExecutionEnding: The body is not JSON, is not a `run_execution_ended`,
            or carries no execution name and status.
    """
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise MalformedExecutionEnding("The record carries no body.")
    try:
        body = json.loads(raw)
    except ValueError as error:
        raise MalformedExecutionEnding("The body is not JSON.") from error
    if not isinstance(body, Mapping) or body.get("kind") != KIND:
        raise MalformedExecutionEnding(f"The body is not a {KIND}.")
    detail = body.get("detail")
    if not isinstance(detail, Mapping):
        raise MalformedExecutionEnding("The body carries no execution detail.")
    run_id = str(detail.get("name", "") or "")
    status = str(detail.get("status", "") or "")
    if not run_id or not status:
        raise MalformedExecutionEnding("The detail carries no execution name and status.")
    return run_id, status, str(detail.get("error", "") or "")


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Finish the run one execution ending left live, and drop its semaphore slot.

    Raises:
        MalformedExecutionEnding: The body is not a usable execution ending, so the
            message parks on the dead letter queue.
    """
    run_id, status, error = parse_body(record)
    if status not in ENDED_STATUSES:
        return
    try:
        run = service.get_run(run_id, consistent=True, settings=settings)
    except service.RunNotFound:
        _log.info(
            "Ignoring an execution ending for a run that does not exist.",
            extra={"event": "runs.execution_ending.ignored", "run_id": run_id},
        )
        return
    service.release_semaphore(run_id, settings=settings)
    if str(run.get("status", "")) in service.TERMINAL_STATUSES:
        return
    cancelled = status == "ABORTED" and error == CANCELLED_ERROR
    service.finish_run(
        run_id,
        "cancelled" if cancelled else "errored",
        error="" if cancelled else f"The run's execution ended {status.replace('_', ' ').lower()}.",
        settings=settings,
    )
    _log.warning(
        "Finished a run its execution left live.",
        extra={"event": "runs.execution_ending.finished", "run_id": run_id, "execution_status": status},
    )


__all__ = ["CANCELLED_ERROR", "ENDED_STATUSES", "KIND", "MalformedExecutionEnding", "handle_record", "parse_body"]
