"""A run's API token is bound to its own workspace, minted for no speculative plan and read-only while planning.

The factory grant, `workspaces:factory`, is the one way a run's token reaches past its
own workspace, creates workspaces or passes step-up; the WebbPulse-Platform workspace
holds it for the factory configuration that manages every other workspace.
"""

import pytest
from fastapi.testclient import TestClient

from app.common.composition.settings import get_settings
from app.common.core.auth import RUN_TOKEN_STEP_UP_CODE, RUN_TOKEN_WORKSPACE_BOUND_CODE
from app.domains.runs import api_credentials
from app.domains.runs import service as runs_service
from tests.conftest import WORKSPACE_PAYLOAD, runner_token

BASE = "/api/v1/runs"
WRITE_GRANT = ["workspaces:read", "workspaces:write", "variables:read", "variables:write", "registry:read"]
FACTORY_GRANT = [*WRITE_GRANT, "registry:write", "workspaces:factory"]
ORG = "/api/v2/organizations/WebbPulse"
NEW_ROLE = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-other"


@pytest.fixture
def api_origin(monkeypatch):
    """A deployment whose API origin is `api.terraform.example.test`."""
    monkeypatch.setenv("API_BASE_URL", "https://api.terraform.example.test/")


@pytest.fixture
def other_workspace(auth_client):
    """A second workspace the run does not belong to."""
    response = auth_client.post("/api/v1/workspaces", json={**WORKSPACE_PAYLOAD, "name": "someone-else"})
    assert response.status_code == 201, response.text
    return response.json()


def _path(workspace_id):
    """A workspace's API path."""
    return f"/api/v1/workspaces/{workspace_id}"


def _grant(auth_client, workspace_id, scopes):
    """Set a workspace's run API token grant through the admin agent key."""
    response = auth_client.patch(_path(workspace_id), json={"run_api_token_scopes": scopes})
    assert response.status_code == 200, response.text


def _bundle(runner_client, run_id):
    """The run's bundle, asserting it was served."""
    response = runner_client.get(f"{BASE}/{run_id}/bundle")
    assert response.status_code == 200, response.text
    return response.json()


def _applying(run_id):
    """Move the run to its apply phase, as a confirmation would."""
    runs_service._update_run(
        run_id, {"status": "applying"}, settings=get_settings(), expected_statuses=frozenset({"planning"})
    )


def _token(auth_client, runner_client, run, scopes, *, phase="applying"):
    """The run's API token for `phase`, under a workspace grant of `scopes`."""
    _grant(auth_client, run["workspace_id"], scopes)
    if phase == "applying":
        _applying(run["run_id"])
    api = _bundle(runner_client, run["run_id"])["api"]
    assert api is not None
    return api["token"]


def _as(app, token):
    """A client presenting `token`."""
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _bound(response):
    """The refusal a run token gets outside its own workspace."""
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == RUN_TOKEN_WORKSPACE_BOUND_CODE


def test_a_run_token_cannot_write_another_workspace(
    app, api_origin, auth_client, created_run, runner_client, other_workspace
):
    """Writes, variable writes and reads of a workspace the run is not in are refused."""
    token = _token(auth_client, runner_client, created_run, WRITE_GRANT)
    other = other_workspace["workspace_id"]

    with _as(app, token) as client:
        _bound(client.patch(_path(other), json={"description": "hijacked"}))
        _bound(client.put(f"{_path(other)}/variables/x", json={"value": "y"}))
        _bound(client.delete(f"{_path(other)}/variables/x"))
        _bound(client.get(_path(other)))
        _bound(client.get(f"{_path(other)}/variables"))
        _bound(client.get(f"{_path(other)}/notification-configurations"))

    assert auth_client.get(_path(other)).json()["description"] == WORKSPACE_PAYLOAD["description"]


def test_a_run_token_still_writes_its_own_workspace(app, api_origin, auth_client, created_run, runner_client):
    """The binding leaves the run's own workspace within its grant."""
    token = _token(auth_client, runner_client, created_run, WRITE_GRANT)
    own = created_run["workspace_id"]

    with _as(app, token) as client:
        assert client.patch(_path(own), json={"description": "managed"}).status_code == 200
        assert client.put(f"{_path(own)}/variables/x", json={"value": "y"}).status_code in (200, 201)


def test_a_run_token_lists_only_its_own_workspace(
    app, api_origin, auth_client, created_run, runner_client, other_workspace
):
    """Listing workspaces, over either API, shows a run token its own workspace and nothing else."""
    token = _token(auth_client, runner_client, created_run, WRITE_GRANT)
    own = created_run["workspace_id"]

    with _as(app, token) as client:
        listed = client.get("/api/v1/workspaces")
        tfe_listed = client.get(f"{ORG}/workspaces")
        by_name = client.get(f"{ORG}/workspaces/{other_workspace['name']}")

    assert listed.status_code == 200, listed.text
    assert [item["workspace_id"] for item in listed.json()["items"]] == [own]
    assert tfe_listed.status_code == 200, tfe_listed.text
    assert [item["id"] for item in tfe_listed.json()["data"]] == [own]
    assert by_name.status_code == 404, by_name.text


def test_a_run_token_cannot_make_list_wide_writes(app, api_origin, auth_client, created_run, runner_client):
    """Creating a workspace or a project names no workspace, so it is refused."""
    token = _token(auth_client, runner_client, created_run, [*WRITE_GRANT, "registry:write"])

    with _as(app, token) as client:
        _bound(client.post("/api/v1/workspaces", json={**WORKSPACE_PAYLOAD, "name": "spawned"}))
        _bound(client.post("/api/v1/projects", json={"name": "spawned"}))
        _bound(client.post("/api/v1/registry/modules", json={}))


def test_a_run_token_never_passes_step_up(app, api_origin, auth_client, created_run, runner_client):
    """A sensitive variable or a run role change in its own workspace needs a person's recent login."""
    token = _token(auth_client, runner_client, created_run, WRITE_GRANT)
    own = created_run["workspace_id"]

    with _as(app, token) as client:
        sensitive = client.put(f"{_path(own)}/variables/secret", json={"value": "v", "sensitive": True})
        role = client.patch(_path(own), json={"pending_run_role_arn": NEW_ROLE})

    for response in (sensitive, role):
        assert response.status_code == 403, response.text
        assert response.json()["error_code"] == RUN_TOKEN_STEP_UP_CODE


def test_the_factory_grant_reaches_every_workspace(
    app, api_origin, auth_client, created_run, runner_client, other_workspace
):
    """The factory's token writes other workspaces, sets sensitive variables and creates workspaces."""
    token = _token(auth_client, runner_client, created_run, FACTORY_GRANT)
    other = other_workspace["workspace_id"]

    with _as(app, token) as client:
        assert client.patch(_path(other), json={"description": "managed"}).status_code == 200
        sensitive = client.put(f"{_path(other)}/variables/secret", json={"value": "v", "sensitive": True})
        assert sensitive.status_code in (200, 201), sensitive.text
        created = client.post("/api/v1/workspaces", json={**WORKSPACE_PAYLOAD, "name": "factory-made"})
        assert created.status_code == 201, created.text
        listed = client.get("/api/v1/workspaces").json()["items"]

    assert len(listed) == 3


def test_a_revoked_factory_grant_binds_a_live_token(
    app, api_origin, auth_client, created_run, runner_client, other_workspace
):
    """Dropping `workspaces:factory` from the grant binds the token at once."""
    token = _token(auth_client, runner_client, created_run, FACTORY_GRANT)
    _grant(auth_client, created_run["workspace_id"], WRITE_GRANT)

    with _as(app, token) as client:
        _bound(client.patch(_path(other_workspace["workspace_id"]), json={"description": "x"}))


def test_only_an_admin_may_grant_the_factory_scope(scoped_client, workspace):
    """The factory grant is set like any other grant: admin plus step-up."""
    with scoped_client("workspaces:read", "workspaces:write") as client:
        response = client.patch(_path(workspace["workspace_id"]), json={"run_api_token_scopes": ["workspaces:factory"]})

    assert response.status_code == 403, response.text


def test_a_factory_plan_reads_every_workspace_but_writes_none(
    app, api_origin, auth_client, created_run, runner_client, other_workspace
):
    """The plan phase keeps the factory's reach for refresh, without any write scope."""
    token = _token(auth_client, runner_client, created_run, FACTORY_GRANT, phase="planning")
    other = other_workspace["workspace_id"]

    with _as(app, token) as client:
        assert client.get(_path(other)).status_code == 200
        assert client.get(f"{_path(other)}/variables").status_code == 200
        assert client.patch(_path(other), json={"description": "x"}).status_code == 403
        assert client.put(f"{_path(other)}/variables/x", json={"value": "y"}).status_code == 403


def test_the_plan_phase_token_is_read_only(app, api_origin, auth_client, created_run, runner_client):
    """While planning, the token carries the grant's reads and no write."""
    _grant(auth_client, created_run["workspace_id"], FACTORY_GRANT)
    api = _bundle(runner_client, created_run["run_id"])["api"]

    assert api["scopes"] == ["workspaces:read", "variables:read", "registry:read", "workspaces:factory"]
    with _as(app, api["token"]) as client:
        assert client.patch(_path(created_run["workspace_id"]), json={"description": "x"}).status_code == 403


@pytest.mark.parametrize(
    "changes", [{"add": 1, "change": 0, "destroy": 0}, {"add": 0, "change": 0, "destroy": 0}], ids=["changes", "none"]
)
def test_the_plan_phase_token_is_revoked_when_planning_ends(
    app, api_origin, auth_client, created_run, runner_client, changes
):
    """Whether the run then waits for confirmation or finishes, the plan's token is dead."""
    token = _token(auth_client, runner_client, created_run, WRITE_GRANT, phase="planning")
    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code == 200

    runs_service.record_phase_result(
        created_run["run_id"], {"phase": "plan", "exit_code": 0, "changes": changes, "error": ""}
    )

    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code == 401


def test_a_failed_plan_revokes_its_token(app, api_origin, auth_client, created_run, runner_client):
    """An errored plan's token is dead too."""
    token = _token(auth_client, runner_client, created_run, WRITE_GRANT, phase="planning")

    runs_service.record_phase_result(
        created_run["run_id"], {"phase": "plan", "exit_code": 1, "changes": {}, "error": "boom"}
    )

    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code == 401


def test_a_plan_only_run_gets_no_token(app, api_origin, auth_client, plan_only_run):
    """A speculative run's bundle carries no API token, whatever the grant."""
    _grant(auth_client, plan_only_run["workspace_id"], FACTORY_GRANT)
    run_id = plan_only_run["run_id"]

    with _as(app, runner_token(run_id)) as client:
        response = client.get(f"{BASE}/{run_id}/bundle")

    assert response.status_code == 200, response.text
    assert response.json()["api"] is None


@pytest.mark.parametrize("status", ["planning", "applying"])
def test_a_pull_request_plan_gets_no_token(api_origin, created_run, status):
    """A run from a pull request is speculative, so nothing is minted in either phase."""
    run = {**created_run, "status": status, "source": "vcs_pr", "plan_only": True}
    workspace = {"run_api_token_scopes": FACTORY_GRANT}

    assert api_credentials.issue(run, workspace, settings=get_settings()) is None
    assert api_credentials.issue({**run, "plan_only": False}, workspace, settings=get_settings()) is None


def test_a_person_is_not_bound(auth_client, created_run, other_workspace):
    """The binding concerns run tokens only; an agent key still reaches every workspace."""
    assert auth_client.get(_path(other_workspace["workspace_id"])).status_code == 200
    assert len(auth_client.get("/api/v1/workspaces").json()["items"]) == 2
