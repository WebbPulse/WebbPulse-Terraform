"""The one events route, routing each record to the consumer its `kind` names.

The Lambda Web Adapter posts every non-HTTP invocation to a single
`AWS_LWA_PASS_THROUGH_PATH`, so however many queues feed this function they all
arrive on one path and only one route can serve it. This module owns that route and
each consumer keeps only its `handle_record`, which is also what lets the suite drive
either consumer directly.

Routing is on the body's `kind`, the field the state machine already stamps on a
confirmation and the EventBridge rule's input transformer stamps on a task stop. An
unknown or missing kind raises, so an unrecognised message parks on its queue's dead
letter queue rather than being silently acknowledged. A record from the runs table's
stream has no body at all, and goes by its `eventSource` to the reports consumer and
the endings consumer both.
A CloudFormation custom resource request arrives raw from the AWS connect topic,
with no `kind` either, and goes to the connect consumer by its shape.

The batch loop is this module's rather than the shared per record one, for one reason:
a consumer that raises an expected "not yet" (a cleanup waiting on its workspace's
delete) is retried like any failure, but logged as the routine wait it is, without a
traceback, so the logs keep ERROR for faults.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping

from fastapi import APIRouter
from webbpulse.events import batch_item_failures, event_records, record_id, register_stream_consumer

from ....common.composition.settings import Settings
from ....common.workspaces import cleanup
from . import aws_connect, confirmations, endings, ingest, reports, task_failures, webhooks

_log = logging.getLogger(__name__)

Handler = Callable[[Mapping[str, Any], Settings | None], None]


LOG_EVENT = "runs.events.batch"
"""The structured `event` every batch summary is logged under."""

RETRY_LATER: tuple[type[Exception], ...] = (cleanup.WorkspaceStillPresent,)
"""Exceptions that mean "not yet" rather than a fault: retried, but logged at info."""


class UnknownRecordKind(Exception):
    """The record names no consumer on this function."""


def _kind(record: Mapping[str, Any]) -> str:
    """The `kind` one record's body carries, or an empty string when it has none.

    Deliberately forgiving: a body this cannot read is not rejected here but handed
    to no handler, and `route_record` raises on that, so one unreadable body fails
    with the same message whatever made it unreadable.
    """
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        body = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(body, Mapping):
        return ""
    return str(body.get("kind", "") or "")


def _ingest(record: Mapping[str, Any], settings: Settings | None) -> None:
    """Hand one upload to the ingest consumer, discarding the run ids it returns."""
    ingest.handle_record(record, settings=settings)


def _webhook(record: Mapping[str, Any], settings: Settings | None) -> None:
    """Hand one verified GitHub delivery to the webhooks consumer, discarding the upload id."""
    webhooks.handle_record(record, settings=settings)


HANDLERS: dict[str, Handler] = {
    confirmations.CONFIRMATION_KIND: lambda record, settings: confirmations.handle_record(record, settings=settings),
    task_failures.TASK_FAILURE_KIND: lambda record, settings: task_failures.handle_record(record, settings=settings),
    ingest.INGEST_KIND: lambda record, settings: _ingest(record, settings),
    cleanup.CLEANUP_KIND: lambda record, settings: cleanup.handle_record(record, settings=settings),
    webhooks.KIND: lambda record, settings: _webhook(record, settings),
}
"""Each `kind` this function consumes, against the consumer that owns it."""


def route_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Hand one record to the consumer its `kind` names, a stream record to reports, or a
    CloudFormation request to the connect consumer.

    Raises:
        UnknownRecordKind: The body names no consumer, so the record is retried and
            eventually parked rather than acknowledged unhandled.
    """
    if reports.is_stream_record(record):
        reports.handle_record(record, settings=settings)
        endings.handle_record(record, settings=settings)
        return
    if aws_connect.is_connect_request(record):
        aws_connect.handle_record(record, settings=settings)
        return
    kind = _kind(record)
    handler = HANDLERS.get(kind)
    if handler is None:
        raise UnknownRecordKind(f"No consumer handles a {kind or 'kindless'} record.")
    handler(record, settings)


def consume_batch(event: Mapping[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Route every record in one batch, returning the failures the mapping should retry.

    A record that raises is reported as a batch item failure either way. One that
    raises a `RETRY_LATER` exception is logged at info without a traceback; any other
    exception is logged with its traceback, as the shared consumer would.
    """
    records = event_records(event)
    failures: list[str] = []
    waiting = 0
    for record in records:
        try:
            route_record(record, settings=settings)
        except RETRY_LATER as error:
            waiting += 1
            _log.info(
                "A record is not ready yet; the event source mapping will retry it.",
                extra={
                    "event": f"{LOG_EVENT}.record_deferred",
                    "record_id": record_id(record),
                    "reason": type(error).__name__,
                },
            )
            failures.append(record_id(record))
        except Exception:
            _log.exception(
                "Handling a record failed; the event source mapping will retry it.",
                extra={"event": f"{LOG_EVENT}.record_failed", "record_id": record_id(record)},
            )
            failures.append(record_id(record))
    _log.info(
        "Handled an events batch.",
        extra={
            "event": LOG_EVENT,
            "records": len(records),
            "handled": len(records) - len(failures),
            "deferred": waiting,
            "failed": len(failures) - waiting,
        },
    )
    return dict(batch_item_failures(failures))


def build_router(settings: Settings | None = None) -> APIRouter:
    """The events router, mounted unprefixed at the adapter's pass-through path.

    `settings` is closed over rather than taken as a FastAPI dependency, because the
    route is registered by the shared package and its signature is not this domain's
    to extend; passing one in is what lets a test drive the consumers against moto's
    tables.
    """
    router = APIRouter()

    def consume(event: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one batch against this domain's settings."""
        return consume_batch(event, settings=settings)

    register_stream_consumer(router, consume, per_record=False, log_event=LOG_EVENT)
    return router


__all__ = [
    "HANDLERS",
    "LOG_EVENT",
    "RETRY_LATER",
    "Handler",
    "UnknownRecordKind",
    "build_router",
    "consume_batch",
    "route_record",
]
