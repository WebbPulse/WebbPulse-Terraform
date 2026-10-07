"""The notifications consumer: run transitions on the runs stream, queued as deliveries.

The stream is the one place every status write passes, whether the API, a consumer or
the state machine made it, so it is where a notification is decided. This consumer
only enqueues, one message per matching configuration, and the queue consumer
delivers each with its own retries.

It never raises. A notification is a side effect of a run, and a fault here must not
hold up the shard or retry the endings consumer that runs before it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from webbpulse.events import deserialize_image

from ....common.composition.settings import Settings, get_settings
from ....common.db.tables import RUNS_COLLECTION
from ....common.notifications import delivery
from ....common.notifications.triggers import trigger_for

_log = logging.getLogger(__name__)

KIND = delivery.MESSAGE_KIND


def notification_event(record: Mapping[str, Any]) -> tuple[dict[str, Any], str] | None:
    """The run image and trigger a stream record fires, or `None` when it fires nothing.

    Only real run rows count: the semaphore row shares the table but carries no
    `collection`.
    """
    event_name = str(record.get("eventName", ""))
    if event_name not in ("INSERT", "MODIFY"):
        return None
    new = deserialize_image(record, "NewImage")
    if new.get("collection") != RUNS_COLLECTION or not new.get("workspace_id") or not new.get("status"):
        return None
    old = deserialize_image(record, "OldImage") if event_name == "MODIFY" else {}
    old_status = str(old.get("status")) if old.get("status") else None
    trigger = trigger_for(event_name, old_status, str(new["status"]))
    if trigger is None:
        return None
    return dict(new), trigger


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Queue one delivery per configuration the transition matches, never raising."""
    try:
        event = notification_event(record)
        if event is None:
            return
        run, trigger = event
        resolved = settings or get_settings()
        messages = delivery.messages_for(
            str(run["workspace_id"]),
            str(run["run_id"]),
            trigger=trigger,
            status=str(run["status"]),
            updated_at=str(run.get("updated_at") or "") or None,
            settings=resolved,
        )
        delivery.enqueue(messages, settings=resolved)
    except Exception:
        _log.exception(
            "Queueing run notifications failed; the transition sends none.",
            extra={"event": "runs.notifications.enqueue_failed"},
        )


def handle_message(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Deliver one queued notification from its SQS record."""
    delivery.handle_message(json.loads(str(record.get("body") or "{}")), settings=settings)


__all__ = ["KIND", "handle_message", "handle_record", "notification_event"]
