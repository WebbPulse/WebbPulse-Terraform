"""Ending a workspace's runs before the workspace itself is deleted.

Deleting a workspace leaves its runs alone: `delete_workspace` never touches the runs
table, and the workspaces function's role is read only on it. A run left `planning` or
`applying` therefore keeps a state machine execution and its Terraform process alive
long after the case that started it, which is exactly the billable leftover the suite
must not produce.

So the e2e side ends them. Everything here answers with a description of what it could
not do rather than raising, because it runs inside cleanup, where one stuck run must
never stop the rest of the sweep.
"""

from __future__ import annotations

import time
from typing import Any

TERMINAL_STATUSES = frozenset({"applied", "planned_and_finished", "errored", "cancelled", "discarded"})

DISCARDABLE_STATUSES = frozenset({"planned", "awaiting_confirmation"})

END_TIMEOUT_SECONDS = 60

END_POLL_SECONDS = 2


def _run_items(api: Any, workspace_id: str) -> list[dict[str, Any]]:
    """Every run of one workspace, or an empty list when the read fails.

    A workspace that was deleted, or one whose runs cannot be read, is not a cleanup
    failure: there is simply nothing to end.
    """
    try:
        response = api.get("/api/v1/runs", params={"workspace_id": workspace_id})
    except Exception:
        return []
    if response.status_code != 200:
        return []
    try:
        body = response.json()
    except ValueError:
        return []
    items = body.get("items") if isinstance(body, dict) else body
    return [dict(item) for item in items] if isinstance(items, list) else []


def _status(api: Any, run_id: str) -> str:
    """One run's current status, or an empty string when it cannot be read."""
    try:
        response = api.get(f"/api/v1/runs/{run_id}")
    except Exception:
        return ""
    if response.status_code == 404:
        return "discarded"
    if response.status_code != 200:
        return ""
    try:
        return str(dict(response.json()).get("status", ""))
    except ValueError:
        return ""


def _post(api: Any, run_id: str, verb: str) -> int:
    """Send one end verb and answer with its status code, or 0 when the call failed."""
    try:
        return int(api.post(f"/api/v1/runs/{run_id}/{verb}").status_code)
    except Exception:
        return 0


def _end(api: Any, run_id: str, status: str) -> None:
    """Ask for the run to end, trying the other verb when the first is refused.

    A plan awaiting a decision is discarded rather than cancelled, because a discard
    fails the confirmation task token and lets the state machine run its own terminal
    path, while a cancel stops the execution and skips it. Every other non-terminal
    status is cancelled. A 409 means the run moved between the list and this call, so
    the other verb is tried once.
    """
    verbs = ("discard", "cancel") if status in DISCARDABLE_STATUSES else ("cancel", "discard")
    for verb in verbs:
        code = _post(api, run_id, verb)
        if code in (200, 202, 204, 404):
            return


def _wait_for_terminal(api: Any, run_id: str) -> str:
    """Poll one run until it is terminal, answering with the last status seen."""
    deadline = time.monotonic() + END_TIMEOUT_SECONDS
    status = _status(api, run_id)
    while status not in TERMINAL_STATUSES and time.monotonic() < deadline:
        time.sleep(END_POLL_SECONDS)
        status = _status(api, run_id)
    return status


def end_runs_for_workspace(api: Any, workspace_id: str) -> list[str]:
    """End every non-terminal run of one workspace and wait for it to settle.

    Answers with a description of each run that did not reach a terminal status inside
    the bounded wait, so the caller can report it. A workspace with no runs, which is
    every workspace when `E2E_RUN_ROLE_ARN` is unset and the run cases are skipped,
    answers with an empty list without sending anything.
    """
    unfinished: list[str] = []
    for item in _run_items(api, workspace_id):
        run_id = str(item.get("run_id", ""))
        status = str(item.get("status", ""))
        if not run_id or status in TERMINAL_STATUSES:
            continue
        try:
            _end(api, run_id, status)
            final = _wait_for_terminal(api, run_id)
        except Exception as error:
            unfinished.append(f"{run_id} ({type(error).__name__})")
            continue
        if final not in TERMINAL_STATUSES:
            unfinished.append(f"{run_id} ({final or 'unreadable'})")
    return unfinished
