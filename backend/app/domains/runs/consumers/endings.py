"""The endings consumer: settles a run the state machine ended behind the API's back.

`MarkErrored`, `MarkApplied` and `MarkPlannedAndFinished` write a terminal status
straight to the runs table, so the only place those endings surface is the table's
stream. The event source mapping delivers a modify whose new image is terminal and
carries no `finished_at`, which every ending the API writes through `finish_run`
already has, and this hands the run to `service.settle_run`.

Unlike the reports consumer this one raises on a fault, so the record is retried by
the event source mapping: a revocation or a queue promotion that never happens is
worth holding the shard for a moment.
"""

from __future__ import annotations

from typing import Any, Mapping

from webbpulse.events import deserialize_image

from ....common.composition.settings import Settings
from .. import service


def settleable_run_id(record: Mapping[str, Any]) -> str | None:
    """The run a stream record shows the state machine ending, or `None` for anything else."""
    if record.get("eventName") != "MODIFY":
        return None
    new = deserialize_image(record, "NewImage")
    run_id = str(new.get("run_id", "") or "")
    if not run_id or str(new.get("status", "")) not in service.TERMINAL_STATUSES:
        return None
    if new.get("finished_at"):
        return None
    return run_id


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> bool:
    """Settle the run one stream record ends. Returns whether a run was settled."""
    run_id = settleable_run_id(record)
    if run_id is None:
        return False
    return service.settle_run(run_id, settings=settings) is not None


__all__ = ["handle_record", "settleable_run_id"]
