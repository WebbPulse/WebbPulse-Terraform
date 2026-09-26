"""Listing runs across every workspace: authorization, ordering and pagination."""

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import ALL_SCOPES, RUNS_READ
from app.common.db.tables import SEMAPHORE_RUN_ID
from app.domains.runs import service as runs_service
from tests.conftest import WORKSPACE_PAYLOAD, mint_key

BASE = "/api/v1/runs"


@pytest.fixture
def two_workspaces_with_runs(auth_client, state_machine):
    """Two workspaces, each holding two finished plan-only runs."""
    from app.domains.workspaces import service as workspaces_service

    built: list[dict] = []
    for index in range(2):
        workspace = auth_client.post("/api/v1/workspaces", json={**WORKSPACE_PAYLOAD, "name": f"listing-{index}"})
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
                json={"workspace_id": workspace_id, "config_version_id": config_version_id, "plan_only": True},
            )
            assert created.status_code == 201, created.text
            run_id = created.json()["run_id"]
            run_ids.append(run_id)
            runs_service.finish_run(run_id, status="planned_and_finished")

        built.append({"workspace_id": workspace_id, "run_ids": run_ids})
    return built


def every_run_id(built: list[dict]) -> list[str]:
    """Every fixture run id, newest first, since a ULID orders by creation time."""
    return sorted((run_id for workspace in built for run_id in workspace["run_ids"]), reverse=True)


def read_every_page(client: TestClient, limit: int) -> list[str]:
    """Follow `next_cursor` to the end and return every run id seen, in order."""
    seen: list[str] = []
    params: dict = {"limit": limit}
    for _ in range(100):
        response = client.get(BASE, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["run_id"] for item in page["items"])
        if page["next_cursor"] is None:
            return seen
        params = {"limit": limit, "cursor": page["next_cursor"]}
    raise AssertionError("pagination did not terminate")


def test_the_scoped_list_is_unchanged(auth_client, two_workspaces_with_runs):
    """Naming a workspace returns that workspace's runs in full, with no cursor."""
    first, second = two_workspaces_with_runs
    page = auth_client.get(BASE, params={"workspace_id": first["workspace_id"]}).json()

    assert [item["run_id"] for item in page["items"]] == sorted(first["run_ids"], reverse=True)
    assert page["next_cursor"] is None


def test_the_unscoped_list_spans_every_workspace_newest_first(auth_client, two_workspaces_with_runs):
    """Omitting the workspace returns every workspace's runs, newest first."""
    items = auth_client.get(BASE).json()["items"]
    assert [item["run_id"] for item in items] == every_run_id(two_workspaces_with_runs)


def test_the_unscoped_list_renders_full_runs(auth_client, two_workspaces_with_runs):
    """A run from the index is the same object the run's own route returns."""
    item = auth_client.get(BASE).json()["items"][0]
    assert item == auth_client.get(f"{BASE}/{item['run_id']}").json()
    assert "collection" not in item


def test_the_unscoped_list_never_returns_the_semaphore(auth_client, created_run):
    """The reserved row carries no `collection`, so the recency index cannot hold it."""
    runs_service.release_semaphore(created_run["run_id"])
    listed = [item["run_id"] for item in auth_client.get(BASE).json()["items"]]
    assert listed == [created_run["run_id"]]
    assert SEMAPHORE_RUN_ID not in listed


@pytest.mark.parametrize("limit", [1, 3, 4, 200])
def test_the_unscoped_list_pages_without_gaps_or_repeats(auth_client, two_workspaces_with_runs, limit):
    """Following the cursor at any page size yields every run exactly once, in order."""
    assert read_every_page(auth_client, limit) == every_run_id(two_workspaces_with_runs)


def test_a_page_holds_at_most_limit_runs(auth_client, two_workspaces_with_runs):
    """`limit` bounds the page and a cursor is returned while runs remain."""
    page = auth_client.get(BASE, params={"limit": 3}).json()
    assert len(page["items"]) == 3
    assert page["next_cursor"] == page["items"][-1]["run_id"]


def test_the_default_page_size_applies(auth_client, created_run, monkeypatch):
    """Without `limit` the page holds the default number of runs."""
    monkeypatch.setattr(runs_service, "DEFAULT_RUN_PAGE_SIZE", 1)
    page = auth_client.get(BASE).json()
    assert len(page["items"]) == 1


def test_an_empty_environment_lists_nothing(auth_client, workspace):
    """No runs is an empty page, not an error."""
    response = auth_client.get(BASE)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "cursor",
    ["not-a-cursor", "run-semaphore", "ws-01JBQ0000000000000000000AA", "run-01jbq0000000000000000000aa", ""],
)
def test_a_malformed_cursor_is_refused(auth_client, created_run, cursor):
    """Only a well-formed run id is a cursor; anything else is a 422, not a silent restart."""
    assert auth_client.get(BASE, params={"cursor": cursor}).status_code == 422


def test_a_cursor_past_every_run_is_an_empty_page(auth_client, two_workspaces_with_runs):
    """A well-formed cursor older than every run ends the list rather than failing."""
    response = auth_client.get(BASE, params={"cursor": "run-00000000000000000000000000"})
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


def test_a_cursor_names_no_partition(auth_client, two_workspaces_with_runs):
    """A cursor carries only a position, so it cannot steer the query to another partition."""
    newest_first = every_run_id(two_workspaces_with_runs)
    page = auth_client.get(BASE, params={"cursor": newest_first[0]}).json()
    assert [item["run_id"] for item in page["items"]] == newest_first[1:]


@pytest.mark.parametrize("params", [{"limit": 1}, {"cursor": "run-01JBQ0000000000000000000AA"}])
def test_paging_parameters_are_refused_on_the_scoped_list(auth_client, created_run, params):
    """`limit` and `cursor` apply only to the cross-workspace list and are refused, not ignored."""
    response = auth_client.get(BASE, params={"workspace_id": created_run["workspace_id"], **params})
    assert response.status_code == 422


@pytest.mark.parametrize("limit", [0, runs_service.MAX_RUN_PAGE_SIZE + 1])
def test_a_limit_out_of_bounds_is_refused(auth_client, limit):
    """The page size is bounded at the contract."""
    assert auth_client.get(BASE, params={"limit": limit}).status_code == 422


def test_a_malformed_workspace_is_refused_rather_than_widened(auth_client):
    """A bad `workspace_id` is a 422, never a silent fall through to every workspace."""
    assert auth_client.get(BASE, params={"workspace_id": "nonsense"}).status_code == 422


def test_the_scoped_list_is_still_404_for_an_absent_workspace(auth_client):
    """Making the parameter optional did not turn a bad workspace into an empty list."""
    assert auth_client.get(BASE, params={"workspace_id": "ws-01JBQ0000000000000000000AA"}).status_code == 404


def test_both_modes_refuse_a_caller_without_runs_read(app, created_run):
    """Every scope but `runs:read` reaches neither list: the unscoped one adds no bypass."""
    token = mint_key(*(scope for scope in ALL_SCOPES if scope != RUNS_READ))
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.get(BASE).status_code == 403
        assert client.get(BASE, params={"workspace_id": created_run["workspace_id"]}).status_code == 403


def test_both_modes_admit_runs_read_alone(app, created_run):
    """`runs:read` alone reaches both lists: the unscoped one demands nothing extra."""
    token = mint_key(RUNS_READ)
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        unscoped = client.get(BASE)
        scoped = client.get(BASE, params={"workspace_id": created_run["workspace_id"]})
    assert unscoped.status_code == 200, unscoped.text
    assert scoped.status_code == 200, scoped.text
    assert [item["run_id"] for item in unscoped.json()["items"]] == [created_run["run_id"]]


def test_both_modes_refuse_an_unauthenticated_caller(client, created_run):
    """No credential reaches neither list."""
    assert client.get(BASE).status_code == 401
    assert client.get(BASE, params={"workspace_id": created_run["workspace_id"]}).status_code == 401


def test_a_run_token_reaches_neither_list(runner_client, created_run):
    """A runner credential carries no human scope, so it cannot list runs in either mode."""
    assert runner_client.get(BASE).status_code == 403
    assert runner_client.get(BASE, params={"workspace_id": created_run["workspace_id"]}).status_code == 403
