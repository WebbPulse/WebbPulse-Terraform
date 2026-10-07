"""The workspace list's search and sort, and the project runs view."""

from __future__ import annotations

from typing import Any

from app.common.db import repositories
from tests.conftest import WORKSPACE_PAYLOAD
from tests.domains.workspaces.test_workspace_latest_run import seed_run

WORKSPACES = "/api/v1/workspaces"


def create(auth_client, name: str, **extra: Any) -> dict[str, Any]:
    """Create a workspace by name and return it."""
    response = auth_client.post(WORKSPACES, json={**WORKSPACE_PAYLOAD, "name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def names(auth_client, **params: str) -> list[str]:
    """The listed workspace names, in the order returned."""
    response = auth_client.get(WORKSPACES, params=params)
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


def test_without_a_sort_the_list_stays_oldest_first(auth_client):
    """No `sort` keeps the order the list has always had."""
    for name in ("charlie", "alpha", "bravo"):
        create(auth_client, name)
    assert names(auth_client) == ["charlie", "alpha", "bravo"]


def test_name_sorts_both_ways_ignoring_case(auth_client):
    """`name` is A to Z and `-name` Z to A."""
    for name in ("charlie", "Alpha", "bravo"):
        create(auth_client, name)
    assert names(auth_client, sort="name") == ["Alpha", "bravo", "charlie"]
    assert names(auth_client, sort="-name") == ["charlie", "bravo", "Alpha"]


def test_created_sorts_newest_first(auth_client):
    """`-created_at` puts the newest workspace first, the id breaking a tie in the same second."""
    for name in ("first", "second", "third"):
        create(auth_client, name)
    items = auth_client.get(WORKSPACES, params={"sort": "-created_at"}).json()["items"]
    keys = [(item["created_at"], item["workspace_id"]) for item in items]
    assert keys == sorted(keys, reverse=True)


def test_latest_run_sorts_newest_first_and_never_run_last(auth_client):
    """`-latest_run` orders by the newest run's creation, with workspaces that never ran last."""
    old = create(auth_client, "old")
    new = create(auth_client, "new")
    create(auth_client, "idle")
    seed_run(old["workspace_id"], seconds=-600, status="applied")
    seed_run(new["workspace_id"], seconds=5, status="applied")
    assert names(auth_client, sort="-latest_run") == ["new", "old", "idle"]


def test_last_updated_sorts_by_the_latest_change(auth_client):
    """`-updated_at` follows the latest change the list shows."""
    quiet = create(auth_client, "quiet")
    busy = create(auth_client, "busy")
    seed_run(quiet["workspace_id"], seconds=-3600, status="applied", updated_at="2001-01-01T00:00:00Z")
    seed_run(busy["workspace_id"], seconds=5, status="applied", updated_at="2099-01-01T00:00:00Z")
    assert names(auth_client, sort="-updated_at") == ["busy", "quiet"]


def test_status_puts_the_runs_that_need_attention_first(auth_client):
    """Confirmation first, then errored, then running, then settled, then never run."""
    expected = ["waiting", "broken", "running", "done", "idle"]
    statuses = {"done": "applied", "running": "planning", "broken": "errored", "waiting": "awaiting_confirmation"}
    created = {name: create(auth_client, name) for name in ("idle", "done", "running", "broken", "waiting")}
    for name, status in statuses.items():
        seed_run(created[name]["workspace_id"], seconds=1, status=status)
    assert names(auth_client, sort="status") == expected


def test_search_matches_part_of_the_name_and_composes_with_sort(auth_client):
    """`search` is a case-insensitive substring, applied before the sort."""
    for name in ("cmp-staging", "cmp-prod", "portfolio-prod"):
        create(auth_client, name)
    assert names(auth_client, search="CMP", sort="name") == ["cmp-prod", "cmp-staging"]
    assert names(auth_client, search="prod", sort="-name") == ["portfolio-prod", "cmp-prod"]


def test_an_unknown_sort_is_refused(auth_client):
    """Only the documented orders are accepted."""
    assert auth_client.get(WORKSPACES, params={"sort": "colour"}).status_code == 422


def seed_listed_run(workspace_id: str, *, seconds: int, status: str) -> dict[str, Any]:
    """A seeded run carrying the config version the run response requires."""
    item = seed_run(workspace_id, seconds=seconds, status=status) | {"config_version_id": "cv-" + "0" * 26}
    repositories.runs().put(item)
    return item


def test_project_runs_merge_the_project_workspaces_newest_first(auth_client):
    """`/runs?project_id=` lists the project's runs only, newest first, capped by `limit`."""
    project = auth_client.post("/api/v1/projects", json={"name": "Runs"}).json()
    inside = create(auth_client, "inside", project_id=project["project_id"])
    sibling = create(auth_client, "sibling", project_id=project["project_id"])
    outside = create(auth_client, "outside")
    oldest = seed_listed_run(inside["workspace_id"], seconds=-60, status="applied")
    newest = seed_listed_run(sibling["workspace_id"], seconds=5, status="planning")
    seed_listed_run(outside["workspace_id"], seconds=10, status="applied")

    response = auth_client.get("/api/v1/runs", params={"project_id": project["project_id"]})
    assert response.status_code == 200, response.text
    assert [item["run_id"] for item in response.json()["items"]] == [newest["run_id"], oldest["run_id"]]

    capped = auth_client.get("/api/v1/runs", params={"project_id": project["project_id"], "limit": 1})
    assert [item["run_id"] for item in capped.json()["items"]] == [newest["run_id"]]

    paged = {"project_id": project["project_id"], "cursor": newest["run_id"]}
    refused = auth_client.get("/api/v1/runs", params=paged)
    assert refused.status_code == 422
