"""The reports consumer: the runs table's stream, turned into GitHub check runs.

Statuses are written from three places, the API, the ingest consumer and the state
machine's direct DynamoDB updates, so the table's stream is the one place every
transition passes. The event source mapping's filter already limits delivery to
inserts and updates of VCS sourced runs.

A record only says a run changed. The report is built from the run as it is read now,
so a late, duplicate or out of order record reports the current state and nothing
older. A record whose status did not move is dropped, since every other field a run
carries is either reported with its status or not reported at all.

This consumer never raises. Reporting is a side effect of a run and must not hold up
the stream shard, so a fault is logged by `reporting.report_run` and the record is
acknowledged.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from webbpulse.events import deserialize_image

from ....common.composition.settings import Settings
from .. import reporting

_log = logging.getLogger(__name__)

STREAM_EVENT_SOURCE = "aws:dynamodb"
"""The `eventSource` every DynamoDB Streams record carries."""

VCS_SOURCES = frozenset({reporting.SOURCE_PUSH, reporting.SOURCE_PR})


def is_stream_record(record: Mapping[str, Any]) -> bool:
    """Whether a record came from a DynamoDB stream rather than a queue."""
    return record.get("eventSource") == STREAM_EVENT_SOURCE


def reportable_run_id(record: Mapping[str, Any]) -> str | None:
    """The run id a stream record should be reported for, or `None` to drop it."""
    if record.get("eventName") not in ("INSERT", "MODIFY"):
        return None
    new = deserialize_image(record, "NewImage")
    if str(new.get("source", "")) not in VCS_SOURCES or not new.get("run_id"):
        return None
    old = deserialize_image(record, "OldImage")
    if old and str(old.get("status", "")) == str(new.get("status", "")):
        return None
    return str(new["run_id"])


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> bool:
    """Report the run one stream record names. Returns whether anything was posted."""
    try:
        run_id = reportable_run_id(record)
    except Exception as exc:
        _log.warning(
            "Dropped a runs stream record that could not be read.",
            extra={"event": "runs.report.unreadable", "error_type": type(exc).__name__},
        )
        return False
    if run_id is None:
        return False
    return reporting.report_run(run_id, settings=settings)


__all__ = ["STREAM_EVENT_SOURCE", "handle_record", "is_stream_record", "reportable_run_id"]
