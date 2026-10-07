"""The workspace list's newest run per workspace and its "Latest change" time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from webbpulse.dynamodb import Repository, new_ulid

from app.common.db import repositories
from app.common.db.tables import RUNS_BY_RECENCY_INDEX, RUNS_BY_WORKSPACE_INDEX, RUNS_COLLECTION, SEMAPHORE_RUN_ID
from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"


def seed_run(workspace_id: str, *, seconds: int, status: str, updated_at: str | None = None) -> dict[str, Any]:
    """Write a run created `seconds` from now, as the runs domain would, and return it."""
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    created_at = moment.isoformat(timespec="seconds").replace("+00:00", "Z")
    item: dict[str, Any] = {
        "run_id": f"run-{new_ulid(moment)}",
        "workspace_id": workspace_id,
        "collection": RUNS_COLLECTION,
        "status": status,
        "plan_only": False,
        "is_destroy": False,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
    }
    repositories.runs().put(item)
    return item


def listed(auth_client) -> dict[str, dict[str, Any]]:
    """The list's items by workspace name."""
    response = auth_client.get(BASE)
    assert response.status_code == 200, response.text
    return {item["name"]: item for item in response.json()["items"]}


def test_a_workspace_that_never_ran_falls_back_to_its_own_change(auth_client, workspace):
    """No run means no latest run, and the latest change is the workspace's own."""
    item = listed(auth_client)["example"]
    assert item["latest_run"] is None
    assert item["latest_change_at"] == (workspace.get("updated_at") or workspace["created_at"])


def test_a_run_newer_than_the_settings_change_is_the_latest_change(auth_client, workspace):
    """A run after a settings edit moves "Latest change" to the run's time."""
    workspace_id = workspace["workspace_id"]
    patched = auth_client.patch(f"{BASE}/{workspace_id}", json={"description": "Edited."})
    assert patched.status_code == 200, patched.text
    seed_run(workspace_id, seconds=1, status="errored")
    newest = seed_run(workspace_id, seconds=2, status="applied", updated_at="2099-01-01T00:00:00Z")

    item = listed(auth_client)["example"]

    assert item["latest_run"]["run_id"] == newest["run_id"]
    assert item["latest_run"]["status"] == "applied"
    assert item["latest_run"]["changed_at"] == "2099-01-01T00:00:00Z"
    assert item["latest_change_at"] == "2099-01-01T00:00:00Z"
    assert item["latest_change_at"] > patched.json()["updated_at"]


def test_each_workspace_gets_its_own_newest_run(auth_client, workspace):
    """Runs are matched to their workspace, and the semaphore row is never a run."""
    second = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "name": "second"}).json()
    repositories.runs().put({"run_id": SEMAPHORE_RUN_ID, "count": 0})
    first_run = seed_run(workspace["workspace_id"], seconds=1, status="planned_and_finished")
    second_run = seed_run(second["workspace_id"], seconds=2, status="awaiting_confirmation")
    seed_run(second["workspace_id"], seconds=-3600, status="applied")

    items = listed(auth_client)

    assert items["example"]["latest_run"]["run_id"] == first_run["run_id"]
    assert items["second"]["latest_run"]["run_id"] == second_run["run_id"]
    assert items["second"]["latest_run"]["status"] == "awaiting_confirmation"


def test_the_list_reads_runs_once_rather_than_per_workspace(auth_client, workspace, monkeypatch):
    """The newest runs come from one read of `by_recency`, never a query per workspace."""
    for name in ("second", "third"):
        created = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "name": name}).json()
        seed_run(created["workspace_id"], seconds=1, status="applied")
    indexes: list[str | None] = []
    original = Repository.query

    def counting(self: Repository, *args: Any, **kwargs: Any) -> Any:
        indexes.append(kwargs.get("index_name"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Repository, "query", counting)

    items = listed(auth_client)

    assert items["example"]["latest_run"] is None
    assert items["third"]["latest_run"]["status"] == "applied"
    assert indexes.count(RUNS_BY_RECENCY_INDEX) == 1
    assert RUNS_BY_WORKSPACE_INDEX not in indexes
