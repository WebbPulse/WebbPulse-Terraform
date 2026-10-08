"""The control plane API token a run's bundle carries for the WebbPulse provider."""

import boto3
import pytest
from fastapi.testclient import TestClient

from app.common.composition.settings import get_settings
from app.domains.runs import api_credentials
from app.domains.runs import service as runs_service

BASE = "/api/v1/runs"
GATE_PARAMETER = "/webbpulse-terraform-test/origin-verify"
VERSIONS = "/v1/modules/WebbPulse/missing/aws/versions"
GRANT = ["workspaces:read", "workspaces:write", "variables:read", "registry:read"]
PLAN_GRANT = ["workspaces:read", "variables:read", "registry:read"]


@pytest.fixture
def api_origin(monkeypatch):
    """A deployment whose API origin is `api.terraform.example.test`."""
    monkeypatch.setenv("API_BASE_URL", "https://api.terraform.example.test/")


def _workspace_path(run):
    """The run's workspace's API path."""
    return f"/api/v1/workspaces/{run['workspace_id']}"


def _grant(auth_client, run, scopes):
    """Set the workspace's run API token grant through the admin agent key."""
    response = auth_client.patch(_workspace_path(run), json={"run_api_token_scopes": scopes})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def granted(api_origin, auth_client, created_run):
    """A run whose workspace grants its runs `GRANT`."""
    _grant(auth_client, created_run, GRANT)
    return created_run


def _bundle(runner_client, run_id):
    """The run's bundle, asserting it was served."""
    response = runner_client.get(f"{BASE}/{run_id}/bundle")
    assert response.status_code == 200, response.text
    return response.json()


def _applying(run):
    """Move the run to its apply phase, as a confirmation would."""
    return runs_service._update_run(
        run["run_id"], {"status": "applying"}, settings=get_settings(), expected_statuses=frozenset({"planning"})
    )


def _as(app, token):
    """A client presenting `token`."""
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_the_bundle_carries_an_api_token_for_a_granting_workspace(granted, runner_client):
    """The provider gets the API origin, a `wpk_` token that expires and the grant it was minted under."""
    _applying(granted)
    api = _bundle(runner_client, granted["run_id"])["api"]

    assert api["host"] == "https://api.terraform.example.test"
    assert api["token"].startswith("wpk_")
    assert api["expires_at"]
    assert api["scopes"] == GRANT
    assert api["origin_verify"] is None


def test_a_workspace_that_grants_nothing_gets_no_token(api_origin, runner_client, created_run):
    """Runs get no API token unless an admin opted the workspace in."""
    assert _bundle(runner_client, created_run["run_id"])["api"] is None


def test_no_api_origin_means_no_token(monkeypatch, auth_client, runner_client, created_run):
    """Without an origin the provider would have nowhere to send the token, so none is minted."""
    monkeypatch.setenv("API_BASE_URL", "")
    _grant(auth_client, created_run, GRANT)

    assert _bundle(runner_client, created_run["run_id"])["api"] is None


def test_the_plan_phase_token_holds_only_the_grants_reads(granted, runner_client):
    """Planning never writes, so its token carries the grant less its write scopes."""
    assert _bundle(runner_client, granted["run_id"])["api"]["scopes"] == PLAN_GRANT


def test_the_token_holds_the_granted_scopes(app, granted, runner_client):
    """Reads and writes inside the grant work."""
    _applying(granted)
    token = _bundle(runner_client, granted["run_id"])["api"]["token"]

    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code == 200
        assert client.get(f"{_workspace_path(granted)}/variables").status_code == 200
        assert client.patch(_workspace_path(granted), json={"description": "managed"}).status_code == 200
        assert client.get(VERSIONS).status_code == 404


def test_the_token_holds_nothing_outside_the_grant(app, granted, runner_client):
    """Not variable writes, runs, the bundle, a new key or the grant itself."""
    run_id = granted["run_id"]
    token = _bundle(runner_client, run_id)["api"]["token"]

    with _as(app, token) as client:
        assert client.put(f"{_workspace_path(granted)}/variables/x", json={"value": "y"}).status_code == 403
        assert client.get(BASE).status_code == 403
        assert client.get(f"{BASE}/{run_id}/bundle").status_code == 401
        assert client.post("/api/v1/api-keys", json={"name": "escape"}).status_code in (401, 403)
        widened = client.patch(_workspace_path(granted), json={"run_api_token_scopes": [*GRANT, "variables:write"]})
        assert widened.status_code == 403


def test_narrowing_the_grant_narrows_a_live_token(app, auth_client, granted, runner_client):
    """Scopes are read from the workspace on every request, not fixed at mint."""
    _applying(granted)
    token = _bundle(runner_client, granted["run_id"])["api"]["token"]
    _grant(auth_client, granted, ["workspaces:read"])

    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code == 200
        assert client.patch(_workspace_path(granted), json={"description": "x"}).status_code == 403


def test_widening_the_grant_does_not_widen_a_live_token(app, auth_client, granted, runner_client):
    """A token keeps at most the grant it was minted under."""
    _applying(granted)
    token = _bundle(runner_client, granted["run_id"])["api"]["token"]
    _grant(auth_client, granted, [*GRANT, "variables:write"])

    with _as(app, token) as client:
        assert client.put(f"{_workspace_path(granted)}/variables/x", json={"value": "y"}).status_code == 403


def test_clearing_the_grant_kills_a_live_token(app, auth_client, granted, runner_client):
    """A cleared grant leaves the token holding nothing."""
    token = _bundle(runner_client, granted["run_id"])["api"]["token"]
    _grant(auth_client, granted, None)

    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code in (401, 403)


def test_a_newer_bundle_revokes_the_previous_token(app, granted, runner_client):
    """Only the newest API token of a run is live."""
    run_id = granted["run_id"]
    first = _bundle(runner_client, run_id)["api"]["token"]
    second = _bundle(runner_client, run_id)["api"]["token"]

    with _as(app, first) as client:
        assert client.get("/api/v1/workspaces").status_code == 401
    with _as(app, second) as client:
        assert client.get("/api/v1/workspaces").status_code == 200


def test_the_runs_ending_revokes_the_token(app, granted, runner_client):
    """A finished run's API token is dead at once, not at its expiry."""
    run_id = granted["run_id"]
    token = _bundle(runner_client, run_id)["api"]["token"]

    runs_service.finish_run(run_id, "errored", error="stopped")

    with _as(app, token) as client:
        assert client.get("/api/v1/workspaces").status_code == 401


def test_a_run_outside_its_phase_gets_no_token(granted):
    """A run that left its phase before the swap is refused a token."""
    run = runs_service.finish_run(granted["run_id"], "errored", error="stopped")
    workspace = {"run_api_token_scopes": GRANT}

    assert api_credentials.issue(run, workspace, settings=get_settings()) is None


def test_the_hash_never_leaves_the_service(auth_client, granted, runner_client):
    """The run as the API shows it carries no API token hash."""
    run_id = granted["run_id"]
    _bundle(runner_client, run_id)

    body = auth_client.get(f"{BASE}/{run_id}").json()
    assert api_credentials.HASH_ATTRIBUTE not in body


@pytest.fixture
def gate(monkeypatch):
    """A deployment behind the access gate, whose header value lives in SSM."""
    boto3.client("ssm", region_name="us-west-2").put_parameter(
        Name=GATE_PARAMETER, Value="gate-value", Type="SecureString"
    )
    monkeypatch.setenv("ORIGIN_VERIFY_PARAMETER", GATE_PARAMETER)


@pytest.fixture
def missing_gate(monkeypatch):
    """A deployment whose gate parameter cannot be read."""
    monkeypatch.setenv("ORIGIN_VERIFY_PARAMETER", "/missing")


def test_the_bundle_carries_the_gate_header_behind_the_access_gate(gate, granted, runner_client):
    """The runs function reads the gate value, so the runner task role never needs to."""
    assert _bundle(runner_client, granted["run_id"])["api"]["origin_verify"] == "gate-value"


def test_an_unreadable_gate_header_is_left_out(missing_gate, granted, runner_client):
    """A missing parameter still serves the bundle, without the header value."""
    api = _bundle(runner_client, granted["run_id"])["api"]
    assert api["token"].startswith("wpk_")
    assert api["origin_verify"] is None


def test_the_widest_grant_still_cannot_apply(app, auth_client, api_origin, created_run, runner_client):
    """No grant reaches `runs:apply`, so a run's token can never confirm its own apply."""
    from app.common.core.auth import RUN_API_TOKEN_SCOPES

    _grant(auth_client, created_run, list(RUN_API_TOKEN_SCOPES))
    run_id = created_run["run_id"]
    token = _bundle(runner_client, run_id)["api"]["token"]

    with _as(app, token) as client:
        assert client.post(f"{BASE}/{run_id}/confirm").status_code == 403
