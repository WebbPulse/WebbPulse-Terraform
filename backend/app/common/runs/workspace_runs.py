"""A workspace's runs, as its delete and the workspace list see them.

The runs domain owns every other read and write on the runs table. The functions
here are the workspaces domain's whole reach into it: whether a workspace still has
a run going, removing its finished runs, and each workspace's latest run for the
list. The terminal status set lives here so the runs domain reads the same one and
the two cannot disagree on what is finished.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed

from ..composition.settings import Settings, get_settings
from ..db import repositories
from ..db.tables import RUNS_BY_RECENCY_INDEX, RUNS_BY_WORKSPACE_INDEX, RUNS_COLLECTION, SEMAPHORE_RUN_ID

RunStatus = Literal[
    "pending",
    "planning",
    "planned",
    "awaiting_confirmation",
    "applying",
    "applied",
    "planned_and_finished",
    "errored",
    "cancelled",
    "discarded",
]
"""Every state a run can hold, as the contract fixes them. Here so the workspace list
and the runs domain share one set."""

TERMINAL_RUN_STATUSES: Final = frozenset({"applied", "planned_and_finished", "errored", "cancelled", "discarded"})
"""The run statuses that mean a run is finished and holds nothing open."""


LATEST_RUN_FIELDS: Final = (
    "run_id",
    "status",
    "created_at",
    "updated_at",
    "finished_at",
    "plan_only",
    "is_destroy",
)
"""The run fields the workspace list carries for each workspace's latest run."""

_ULID_LENGTH: Final = 26


class RunStillActive(Exception):
    """A run on the workspace has not finished, so the workspace cannot go yet."""

    def __init__(self, run_id: str, status: str) -> None:
        """Carry the run and its status, so a refusal can name both."""
        super().__init__(run_id)
        self.run_id = run_id
        self.status = status


def _workspace_runs(workspace_id: str, *, settings: Settings) -> list[dict[str, Any]]:
    """Every run row on one workspace, newest first, never the semaphore row."""
    return [
        item
        for item in repositories.runs(settings).iter_query(
            Key("workspace_id").eq(workspace_id),
            index_name=RUNS_BY_WORKSPACE_INDEX,
            ascending=False,
        )
        if str(item.get("run_id", "")) and str(item.get("run_id", "")) != SEMAPHORE_RUN_ID
    ]


def workspace_run_ids(workspace_id: str, *, settings: Settings | None = None) -> list[str]:
    """Every run id on the workspace, which is what a delete's object purge walks."""
    resolved = settings or get_settings()
    return [str(item["run_id"]) for item in _workspace_runs(workspace_id, settings=resolved)]


def _parked_saved_plan(item: dict[str, Any]) -> bool:
    """Whether a run is a saved plan awaiting its apply, which holds no lock."""
    return bool(item.get("save_plan", False)) and str(item.get("status", "")) == "awaiting_confirmation"


def require_no_active_run(
    workspace_id: str, *, ignore_plan_only: bool = False, settings: Settings | None = None
) -> None:
    """Raise `RunStillActive` for the newest run on the workspace that is not finished.

    A status outside `TERMINAL_RUN_STATUSES` counts as still going, including one
    this module does not know, so a new status fails closed. `ignore_plan_only`
    passes over plan only runs, which read state without its lock, and saved plans
    parked awaiting their apply, which hold no lock, for a caller that only cares
    about the lock.
    """
    resolved = settings or get_settings()
    for item in _workspace_runs(workspace_id, settings=resolved):
        if ignore_plan_only and (bool(item.get("plan_only", False)) or _parked_saved_plan(item)):
            continue
        status = str(item.get("status", ""))
        if status not in TERMINAL_RUN_STATUSES:
            raise RunStillActive(str(item["run_id"]), status)


def delete_workspace_runs(workspace_id: str, *, settings: Settings | None = None) -> int:
    """Delete every finished run row on the workspace and return how many went.

    Each delete is conditional on the row still belonging to this workspace and
    still being finished, so a run created after `require_no_active_run` looked is
    never removed out from under its execution: its delete fails the condition and
    this raises `RunStillActive` for it instead. A row already gone by then is
    skipped rather than refused. The run's artifacts are purged by the workspace
    cleanup and its logs are left to the log group retention.
    """
    resolved = settings or get_settings()
    repository = repositories.runs(resolved)
    deleted = 0
    for item in _workspace_runs(workspace_id, settings=resolved):
        run_id = str(item["run_id"])
        try:
            repository.delete(
                {"run_id": run_id},
                condition=Attr("workspace_id").eq(workspace_id) & Attr("status").is_in(sorted(TERMINAL_RUN_STATUSES)),
            )
        except ConditionFailed as error:
            current = repository.get({"run_id": run_id})
            if current is None:
                continue
            raise RunStillActive(run_id, str(current.get("status", ""))) from error
        deleted += 1
    return deleted


def _ulid_part(identifier: str) -> str:
    """The ULID an id ends in, or the empty string when it does not end in one."""
    tail = identifier.rsplit("-", 1)[-1]
    return tail.upper() if len(tail) == _ULID_LENGTH else ""


def latest_run_summary(run: dict[str, Any]) -> dict[str, Any]:
    """The part of a run row the workspace list shows, with `changed_at` its latest timestamp."""
    summary = {field: run[field] for field in LATEST_RUN_FIELDS if run.get(field) is not None}
    summary["changed_at"] = str(run.get("updated_at") or run.get("finished_at") or run.get("created_at") or "")
    return summary


def latest_runs(workspace_ids: list[str], *, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """Each named workspace's newest run, from one newest first walk of `by_recency`.

    One paginated read for the whole list rather than a query per workspace. The walk
    stops once every workspace has its run, or once it reaches runs older than every
    workspace still missing one: run and workspace ids are ULIDs and a run is created
    after its workspace, so nothing older can belong to them. A workspace that never
    ran is absent from the result.
    """
    resolved = settings or get_settings()
    missing = {workspace_id: _ulid_part(workspace_id) for workspace_id in workspace_ids}
    found: dict[str, dict[str, Any]] = {}
    if not missing:
        return found
    for item in repositories.runs(resolved).iter_query(
        Key("collection").eq(RUNS_COLLECTION),
        index_name=RUNS_BY_RECENCY_INDEX,
        ascending=False,
    ):
        run_id = str(item.get("run_id", ""))
        workspace_id = str(item.get("workspace_id", ""))
        if run_id and run_id != SEMAPHORE_RUN_ID and workspace_id in missing:
            found[workspace_id] = item
            del missing[workspace_id]
        if not missing:
            break
        run_ulid = _ulid_part(run_id)
        if run_ulid and all(created and run_ulid < created for created in missing.values()):
            break
    return found
