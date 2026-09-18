"""The confirmations consumer: lands the state machine's task token on its run.

A DynamoDB integration cannot carry a Step Functions task token, so the
`AwaitConfirmation` state sends the token to the confirmations queue instead and
this route stores it. Until it lands, `POST /runs/{id}/confirm` has no token to
send success on and answers 409, so the window between the plan finishing and the
message being consumed is the only time a planned run cannot be confirmed.

Raising is how a record is retried. The event source mapping runs with a batch
size of one and `ReportBatchItemFailures`, so a raise returns that one message to
the queue and, once its receives are exhausted, parks it on the dead letter queue
rather than dropping the token silently.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from fastapi import APIRouter
from webbpulse.events import register_stream_consumer

from ....common.composition.settings import Settings
from .. import service

_log = logging.getLogger(__name__)

CONFIRMATION_KIND = "run_confirmation_requested"
"""The `kind` the state machine stamps on a confirmation message. Anything else on
this queue is not ours to act on."""

STORABLE_STATUSES = frozenset({"awaiting_confirmation"})
"""Where a run must be for a token to land on it.

The plan phase result moves the run to `awaiting_confirmation` before the state
machine reaches `AwaitConfirmation`, so that is the status the message arrives
against. A run that was cancelled or discarded in between is in none of these and
the record fails, which parks the message rather than reviving a finished run.
"""


class MalformedConfirmation(Exception):
    """The message body is not a confirmation this consumer can act on."""


def parse_body(record: Mapping[str, Any]) -> tuple[str, str]:
    """The run id and task token one queue record carries.

    Raises:
        MalformedConfirmation: The body is not JSON, is not an object, is not a
            `run_confirmation_requested`, or is missing either field. Each is a
            permanent fault, but it still raises so the message parks on the dead
            letter queue instead of vanishing with a task token nobody holds.
    """
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise MalformedConfirmation("The record carries no body.")

    try:
        body = json.loads(raw)
    except ValueError as error:
        raise MalformedConfirmation("The body is not JSON.") from error

    if not isinstance(body, Mapping):
        raise MalformedConfirmation("The body is not a JSON object.")

    kind = str(body.get("kind", ""))
    if kind != CONFIRMATION_KIND:
        raise MalformedConfirmation(f"The body is a {kind or 'kindless'} message, not a {CONFIRMATION_KIND}.")

    run_id = str(body.get("run_id", "") or "")
    task_token = str(body.get("task_token", "") or "")
    if not run_id:
        raise MalformedConfirmation("The body names no run.")
    if not task_token:
        raise MalformedConfirmation("The body carries no task token.")
    return run_id, task_token


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Store one confirmation's task token against its run.

    The write is conditional on the run still awaiting a confirmation, so a token
    cannot land on a run that was cancelled, discarded or already confirmed while
    the message sat on the queue.

    Raises:
        MalformedConfirmation: The body is not a usable confirmation.
        RunNotFound: No such run, or it is no longer awaiting a confirmation.
    """
    run_id, task_token = parse_body(record)

    service.store_confirm_task_token(
        run_id,
        task_token,
        expected_statuses=STORABLE_STATUSES,
        settings=settings,
    )
    _log.info(
        "Stored a run's confirmation task token.",
        extra={"event": "runs.confirmation.stored", "run_id": run_id},
    )


def build_router(settings: Settings | None = None) -> APIRouter:
    """The consumer's router, mounted at the adapter's pass-through path.

    Unprefixed: the Lambda Web Adapter posts a queue invocation to
    `AWS_LWA_PASS_THROUGH_PATH`, which is outside `/api/v1`, and the HTTP API
    never routes it. `settings` is closed over rather than taken as a FastAPI
    dependency, because the route is registered by the shared package and its
    signature is not this domain's to extend; passing one in is what lets a test
    drive the consumer against moto's tables.
    """
    router = APIRouter()

    def consume(record: Mapping[str, Any]) -> None:
        """Handle one record against this domain's settings."""
        handle_record(record, settings=settings)

    register_stream_consumer(router, consume, log_event="runs.confirmations.batch")
    return router


__all__ = [
    "CONFIRMATION_KIND",
    "STORABLE_STATUSES",
    "MalformedConfirmation",
    "build_router",
    "handle_record",
    "parse_body",
]
