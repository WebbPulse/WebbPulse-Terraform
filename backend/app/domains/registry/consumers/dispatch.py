"""The one events route, routing each record to the consumer its `kind` names.

The Lambda Web Adapter posts every non-HTTP invocation to a single
`AWS_LWA_PASS_THROUGH_PATH`, so this function's queue arrives on one path. An
unknown or missing kind raises, so an unrecognised message parks on the dead letter
queue rather than being silently acknowledged.

In the whole-surface application the runs domain registers the same path first,
so there this route is shadowed; the suite drives `route_record` directly.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from fastapi import APIRouter
from webbpulse.events import register_stream_consumer

from ....common.composition.settings import Settings
from . import tags

Handler = Callable[[Mapping[str, Any], Settings | None], None]


class UnknownRecordKind(Exception):
    """The record names no consumer on this function."""


def _kind(record: Mapping[str, Any]) -> str:
    """The `kind` one record's body carries, or an empty string when it has none."""
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


def _tag(record: Mapping[str, Any], settings: Settings | None) -> None:
    """Hand one tag push to the tag consumer, discarding its outcome."""
    tags.handle_record(record, settings=settings)


HANDLERS: dict[str, Handler] = {tags.KIND: _tag}
"""Each `kind` this function consumes, against the consumer that owns it."""


def route_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Hand one record to the consumer its `kind` names.

    Raises:
        UnknownRecordKind: The body names no consumer.
    """
    kind = _kind(record)
    handler = HANDLERS.get(kind)
    if handler is None:
        raise UnknownRecordKind(f"No consumer handles a {kind or 'kindless'} record.")
    handler(record, settings)


def build_router(settings: Settings | None = None) -> APIRouter:
    """The events router, mounted unprefixed at the adapter's pass-through path."""
    router = APIRouter()

    def consume(record: Mapping[str, Any]) -> None:
        """Handle one record against this domain's settings."""
        route_record(record, settings=settings)

    register_stream_consumer(router, consume, log_event="registry.events.batch")
    return router


__all__ = ["HANDLERS", "Handler", "UnknownRecordKind", "build_router", "route_record"]
