"""The registry credential a run's bundle carries for `terraform init`."""

import pytest
from fastapi.testclient import TestClient

from app.domains.runs import registry_credentials
from app.domains.runs import service as runs_service

BASE = "/api/v1/runs"
VERSIONS = "/v1/modules/WebbPulse/missing/aws/versions"


@pytest.fixture
def frontend(monkeypatch):
    """A deployment whose SPA, and so its module source host, is `terraform.example.test`."""
    monkeypatch.setenv("IDENTITY_FRONTEND_BASE_URL", "https://terraform.example.test")


def _bundle(runner_client, run_id):
    """The run's bundle, asserting it was served."""
    response = runner_client.get(f"{BASE}/{run_id}/bundle")
    assert response.status_code == 200, response.text
    return response.json()


def _registry(app, token):
    """A client presenting `token` to the registry protocol."""
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_the_bundle_carries_a_registry_credential_for_the_spa_host(frontend, runner_client, created_run):
    """The module source host gets a `wpk_` token that expires."""
    registry = _bundle(runner_client, created_run["run_id"])["registry"]

    assert registry["hosts"] == ["terraform.example.test"]
    assert registry["token"].startswith("wpk_")
    assert registry["expires_at"]


def test_no_registry_host_means_no_credential(runner_client, created_run):
    """A deployment with no SPA origin mints nothing."""
    assert _bundle(runner_client, created_run["run_id"])["registry"] is None


def test_the_credential_reads_the_registry(frontend, app, runner_client, created_run):
    """Terraform's versions call gets past the guard: an unknown module is a 404, not a 401."""
    token = _bundle(runner_client, created_run["run_id"])["registry"]["token"]

    with _registry(app, token) as client:
        assert client.get(VERSIONS).status_code == 404


def test_the_credential_opens_nothing_else(frontend, app, runner_client, created_run):
    """Not the bundle, not a workspace listing, not the registry's own management routes."""
    run_id = created_run["run_id"]
    token = _bundle(runner_client, run_id)["registry"]["token"]

    with _registry(app, token) as client:
        assert client.get(f"{BASE}/{run_id}/bundle").status_code == 401
        assert client.get("/api/v1/workspaces").status_code in (401, 403)
        assert client.get("/api/v1/registry/modules").status_code in (401, 403)


def test_the_run_token_itself_cannot_read_the_registry(app, created_run):
    """The runner's own token stays out of the engine's reach and is refused here."""
    with _registry(app, created_run["run_token"]) as client:
        assert client.get(VERSIONS).status_code in (401, 403)


def test_a_newer_bundle_revokes_the_previous_credential(frontend, app, runner_client, created_run):
    """Only the newest registry credential of a run is live."""
    run_id = created_run["run_id"]
    first = _bundle(runner_client, run_id)["registry"]["token"]
    second = _bundle(runner_client, run_id)["registry"]["token"]

    with _registry(app, first) as client:
        assert client.get(VERSIONS).status_code == 401
    with _registry(app, second) as client:
        assert client.get(VERSIONS).status_code == 404


def test_the_runs_ending_revokes_the_credential(frontend, app, runner_client, created_run):
    """A finished run's registry credential is dead at once, not at its expiry."""
    run_id = created_run["run_id"]
    token = _bundle(runner_client, run_id)["registry"]["token"]

    runs_service.finish_run(run_id, "errored", error="stopped")

    with _registry(app, token) as client:
        assert client.get(VERSIONS).status_code == 401


def test_a_run_outside_its_phase_gets_no_credential(frontend, created_run):
    """A run that left its phase before the swap is refused a credential."""
    from app.common.composition.settings import get_settings

    run = runs_service.finish_run(created_run["run_id"], "errored", error="stopped")

    assert registry_credentials.issue(run, settings=get_settings()) is None


def test_the_hash_never_leaves_the_service(frontend, auth_client, runner_client, created_run):
    """The run as the API shows it carries no registry token hash."""
    run_id = created_run["run_id"]
    _bundle(runner_client, run_id)

    body = auth_client.get(f"{BASE}/{run_id}").json()
    assert registry_credentials.HASH_ATTRIBUTE not in body
