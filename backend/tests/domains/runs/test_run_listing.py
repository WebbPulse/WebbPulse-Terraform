"""Listing runs in one workspace and across every workspace, and paging both.

The unscoped list is the one that could leak, so its authorization boundary is
asserted here rather than left to the scoped list's coverage.
"""

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import (
    CONFIGS_WRITE,
    RUNS_READ,
    RUNS_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
)
from app.common.db.tables import RUNS_BY_RECENCY_INDEX, RUNS_BY_WORKSPACE_INDEX
from app.domains.runs import service as runs_service
from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/runs"


@pytest.fixture
def two_workspaces_with_runs(auth_client, state_machine):
    """Two workspaces, each holding two plan-only runs, newest last per workspace.

    Plan-only so each run finishes its own lifecycle rather than queueing behind
    the one before it, which would leave the second `pending` and change nothing
    about what the list has to return but would make the fixture harder to read.
    """
    from app.domains.workspaces import service as workspaces_service

    built: list[dict] = []
    for index in range(2):
        workspace = auth_client.post(
            "/api/v1/workspaces",
            json={**WORKSPACE_PAYLOAD, "name": f"listing-{index}"},
        )
        assert workspace.status_code == 201, workspace.text
        workspace_id = workspace.json()["workspace_id"]

        run_ids: list[str] = []
        for _ in range(2):
            version = auth_client.post(
                f"/api/v1/workspaces/{workspace_id}/config-versions",
                json={"size_bytes": 1024},
            )
            assert version.status_code == 201, version.text
            config_version_id = version.json()["config_version"]["config_version_id"]
            workspaces_service.mark_config_version_uploaded(config_version_id)

            created = auth_client.post(
                BASE,
                json={
                    "workspace_id": workspace_id,
                    "config_version_id": config_version_id,
                    "plan_only": True,
                    "message": "",
                },
            )
            assert created.status_code == 201, created.text
            run_id = created.json()["run_id"]
            run_ids.append(run_id)
            runs_service.finish_run(run_id, status="planned_and_finished")

        built.append({"workspace_id": workspace_id, "run_ids": run_ids})
    return built


def test_the_scoped_list_returns_only_that_workspace(auth_client, two_workspaces_with_runs):
    """Naming a workspace still queries that workspace's index alone."""
    first, second = two_workspaces_with_runs
    items = auth_client.get(BASE, params={"workspace_id": first["workspace_id"]}).json()["items"]

    assert {item["run_id"] for item in items} == set(first["run_ids"])
    assert not {item["run_id"] for item in items} & set(second["run_ids"])


def test_the_unscoped_list_spans_every_workspace(auth_client, two_workspaces_with_runs):
    """Omitting the workspace returns both workspaces' runs in one response."""
    items = auth_client.get(BASE).json()["items"]
    expected = {run_id for built in two_workspaces_with_runs for run_id in built["run_ids"]}

    assert {item["run_id"] for item in items} == expected


def test_the_unscoped_list_is_newest_first(auth_client, two_workspaces_with_runs):
    """Recency ordering holds across workspaces, which is the whole point of the index.

    A run id is a ULID, so descending by id is descending by creation time and the
    expected order is every id sorted backwards regardless of which workspace it
    belongs to.
    """
    items = auth_client.get(BASE).json()["items"]
    every_id = [run_id for built in two_workspaces_with_runs for run_id in built["run_ids"]]

    assert [item["run_id"] for item in items] == sorted(every_id, reverse=True)


def test_the_unscoped_list_carries_the_list_view_attributes(auth_client, two_workspaces_with_runs):
    """The projected index returns what a workspace row renders: status and recency."""
    item = auth_client.get(BASE).json()["items"][0]

    assert item["workspace_id"]
    assert item["status"]
    assert item["created_at"]
    assert item["actor"]["kind"] == "agent"


def test_the_unscoped_list_never_returns_the_semaphore(auth_client, created_run):
    """The reserved row carries no `collection`, so the recency index cannot hold it."""
    from app.common.db.tables import SEMAPHORE_RUN_ID

    runs_service.release_semaphore(created_run["run_id"])
    items = auth_client.get(BASE).json()["items"]

    assert SEMAPHORE_RUN_ID not in {item["run_id"] for item in items}


def test_the_unscoped_list_pages(auth_client, two_workspaces_with_runs):
    """A limited unscoped list hands back a cursor that continues it without repeats."""
    every_id = sorted(
        (run_id for built in two_workspaces_with_runs for run_id in built["run_ids"]),
        reverse=True,
    )

    seen: list[str] = []
    cursor = None
    for _ in range(len(every_id) + 1):
        params: dict = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        page = auth_client.get(BASE, params=params).json()
        seen.extend(item["run_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == every_id
    assert cursor is None


def test_the_scoped_list_pages(auth_client, two_workspaces_with_runs):
    """The per workspace mode pages the same way and stays inside its workspace."""
    first = two_workspaces_with_runs[0]
    expected = sorted(first["run_ids"], reverse=True)

    seen: list[str] = []
    cursor = None
    for _ in range(len(expected) + 1):
        params: dict = {"workspace_id": first["workspace_id"], "limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        page = auth_client.get(BASE, params=params).json()
        seen.extend(item["run_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert seen == expected


def test_the_last_page_carries_no_cursor(auth_client, created_run):
    """A list that fits in one page ends the pagination rather than looping."""
    page = auth_client.get(BASE).json()
    assert page["next_cursor"] is None


def test_a_mangled_cursor_restarts_rather_than_failing(auth_client, two_workspaces_with_runs):
    """A cursor that is not decodable reads as no cursor, which cannot widen a read."""
    response = auth_client.get(BASE, params={"cursor": "not-a-cursor"})
    assert response.status_code == 200, response.text
    assert response.json()["items"]


def test_a_cursor_cannot_escape_the_scoped_query(auth_client, two_workspaces_with_runs):
    """A cursor from the unscoped list does not widen a scoped one past its workspace.

    The key condition is set by the request's `workspace_id` and never by the
    cursor, so the worst a borrowed cursor does is skip rows.
    """
    first, second = two_workspaces_with_runs
    unscoped = auth_client.get(BASE, params={"limit": 1}).json()

    page = auth_client.get(
        BASE,
        params={"workspace_id": first["workspace_id"], "cursor": unscoped["next_cursor"]},
    ).json()

    assert not {item["run_id"] for item in page["items"]} & set(second["run_ids"])


def test_the_unscoped_list_needs_the_same_scope_as_the_scoped_one(app, created_run):
    """`runs:read` guards both modes, and a caller without it reaches neither.

    This is the authorization boundary. The control plane is single tenant with no
    per workspace membership, so the rule the scoped list enforces is a scope and
    nothing else, and the unscoped list enforces exactly that same scope. A caller
    holding it can already list any workspace id it names, so listing them together
    returns no run it could not have assembled one workspace at a time.
    """
    from tests.conftest import mint_key

    token = mint_key(WORKSPACES_READ, WORKSPACES_WRITE, CONFIGS_WRITE, RUNS_WRITE)
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.get(BASE).status_code == 403
        assert client.get(BASE, params={"workspace_id": created_run["workspace_id"]}).status_code == 403


def test_the_unscoped_list_refuses_an_unauthenticated_caller(client):
    """No credential reaches neither mode."""
    assert client.get(BASE).status_code == 401


def test_the_unscoped_list_admits_a_read_only_caller(app, created_run):
    """`runs:read` alone is enough, the same as for the scoped list."""
    from tests.conftest import mint_key

    token = mint_key(RUNS_READ)
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.get(BASE)
        assert response.status_code == 200, response.text
        assert [item["run_id"] for item in response.json()["items"]] == [created_run["run_id"]]


def test_a_limit_over_the_ceiling_is_refused(auth_client):
    """The page size is clamped at the contract rather than left to the caller."""
    assert auth_client.get(BASE, params={"limit": runs_service.MAX_RUN_PAGE_SIZE + 1}).status_code == 422


def test_a_zero_limit_is_refused(auth_client):
    """A page of nothing would page forever, so it is rejected at the boundary."""
    assert auth_client.get(BASE, params={"limit": 0}).status_code == 422


def test_the_scoped_list_is_still_404_for_an_absent_workspace(auth_client):
    """Making the parameter optional did not turn a bad workspace into an empty list."""
    response = auth_client.get(BASE, params={"workspace_id": "ws-01JBQ0000000000000000000AA"})
    assert response.status_code == 404


def test_the_unscoped_list_is_empty_when_nothing_ran(auth_client, workspace):
    """An environment with no runs lists nothing rather than failing on the index."""
    response = auth_client.get(BASE)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


def test_the_service_lists_across_workspaces(two_workspaces_with_runs):
    """The service call underneath returns the same set, without a route in the way."""
    items, cursor = runs_service.list_all_runs()
    expected = {run_id for built in two_workspaces_with_runs for run_id in built["run_ids"]}

    assert {item["run_id"] for item in items} == expected
    assert cursor is None


def test_a_cursor_from_the_other_index_reads_as_no_cursor():
    """The two indexes key differently, so neither one's cursor starts the other.

    DynamoDB rejects a start key that is not the queried index's own key shape, so
    an unvalidated foreign cursor would be a 500 rather than a refused read. It
    decodes to `None` instead, which restarts the listing.
    """
    recency = runs_service.encode_cursor({"run_id": "run-x", "collection": "run"})

    assert runs_service.decode_cursor(recency, index_name=RUNS_BY_RECENCY_INDEX) is not None
    assert runs_service.decode_cursor(recency, index_name=RUNS_BY_WORKSPACE_INDEX) is None
