"""A workspace's runs, as its delete sees them: still going, or finished and removable.

The runs domain owns every other read and write on the runs table. The two
functions here are the workspaces domain's whole reach into it, and the terminal
status set lives here so the runs domain reads the same one and the two cannot
disagree on what is finished.
"""

from __future__ import annotations

from typing import Any, Final

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed

from ..composition.settings import Settings, get_settings
from ..db import repositories
from ..db.tables import RUNS_BY_WORKSPACE_INDEX, SEMAPHORE_RUN_ID

TERMINAL_RUN_STATUSES: Final = frozenset({"applied", "planned_and_finished", "errored", "cancelled", "discarded"})
"""The run statuses that mean a run is finished and holds nothing open."""


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


def require_no_active_run(workspace_id: str, *, settings: Settings | None = None) -> None:
    """Raise `RunStillActive` for the newest run on the workspace that is not finished.

    A status outside `TERMINAL_RUN_STATUSES` counts as still going, including one
    this module does not know, so a new status fails closed.
    """
    resolved = settings or get_settings()
    for item in _workspace_runs(workspace_id, settings=resolved):
        status = str(item.get("status", ""))
        if status not in TERMINAL_RUN_STATUSES:
            raise RunStillActive(str(item["run_id"]), status)


def delete_workspace_runs(workspace_id: str, *, settings: Settings | None = None) -> int:
    """Delete every finished run row on the workspace and return how many went.

    Each delete is conditional on the row still belonging to this workspace and
    still being finished, so a run created after `require_no_active_run` looked is
    never removed out from under its execution: its delete fails the condition and
    this raises `RunStillActive` for it instead. A row already gone by then is
    skipped rather than refused. The run's artifacts and logs are
    left to the bucket lifecycle and the log group retention.
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
