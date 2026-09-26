"""The one events route, routing each record to the consumer its `kind` names.

The Lambda Web Adapter posts every non-HTTP invocation to a single
`AWS_LWA_PASS_THROUGH_PATH`, so however many queues feed this function they all
arrive on one path and only one route can serve it. This module owns that route and
each consumer keeps only its `handle_record`, which is also what lets the suite drive
either consumer directly.

Routing is on the body's `kind`, the field the state machine already stamps on a
confirmation and the EventBridge rule's input transformer stamps on a task stop. An
unknown or missing kind raises, so an unrecognised message parks on its queue's dead
letter queue rather than being silently acknowledged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping

from fastapi import APIRouter
from webbpulse.events import register_stream_consumer

from ....common.composition.settings import Settings
from . import confirmations, ingest, task_failures

_log = logging.getLogger(__name__)

Handler = Callable[[Mapping[str, Any], Settings | None], None]


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


HANDLERS: dict[str, Handler] = {
    confirmations.CONFIRMATION_KIND: lambda record, settings: confirmations.handle_record(record, settings=settings),
    task_failures.TASK_FAILURE_KIND: lambda record, settings: task_failures.handle_record(record, settings=settings),
    ingest.INGEST_KIND: lambda record, settings: _ingest(record, settings),
}
"""Each `kind` this function consumes, against the consumer that owns it."""


def route_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Hand one record to the consumer its `kind` names.

    Raises:
        UnknownRecordKind: The body names no consumer, so the record is retried and
            eventually parked rather than acknowledged unhandled.
    """
    kind = _kind(record)
    handler = HANDLERS.get(kind)
    if handler is None:
        raise UnknownRecordKind(f"No consumer handles a {kind or 'kindless'} record.")
    handler(record, settings)


def build_router(settings: Settings | None = None) -> APIRouter:
    """The events router, mounted unprefixed at the adapter's pass-through path.

    `settings` is closed over rather than taken as a FastAPI dependency, because the
    route is registered by the shared package and its signature is not this domain's
    to extend; passing one in is what lets a test drive the consumers against moto's
    tables.
    """
    router = APIRouter()

    def consume(record: Mapping[str, Any]) -> None:
        """Handle one record against this domain's settings."""
        route_record(record, settings=settings)

    register_stream_consumer(router, consume, log_event="runs.events.batch")
    return router


__all__ = ["HANDLERS", "Handler", "UnknownRecordKind", "build_router", "route_record"]
