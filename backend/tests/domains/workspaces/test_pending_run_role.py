"""A staged run role: kept apart from the working one until a verification run proves it.

A half finished quick setup must not break a workspace that works, so a new role
is staged as `pending_run_role_arn`, runs keep the current role, and only the
recording check switches over once a `run_role_check` run assumed the staged one.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domains.runs import service as runs_service
from app.domains.workspaces import run_role_check
from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"
RUNS = "/api/v1/runs"
CURRENT = WORKSPACE_PAYLOAD["run_role_arn"]
STAGED = "arn:aws:iam::210987654321:role/webbpulse-terraform-test-workspace-staged"
STAGED_ACCOUNT = "210987654321"


def _stage(auth_client, workspace_id: str, role_arn: str | None = STAGED) -> dict[str, Any]:
    """Stage `role_arn` on the workspace, or discard the staged role with `None`."""
    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"pending_run_role_arn": role_arn})
    assert response.status_code == 200, response.text
    return response.json()


def _start_run(auth_client, workspace_id: str, config_version_id: str, **body: Any):
    """Create a run on the workspace with `body` merged into the create request."""
    return auth_client.post(
        RUNS,
        json={"workspace_id": workspace_id, "config_version_id": config_version_id, **body},
    )


def _finish_plan(run_id: str) -> None:
    """Record a successful plan, which is only reachable after the runner assumed the role."""
    runs_service.record_phase_result(
        run_id,
        {"phase": "plan", "exit_code": 0, "changes": {"add": 0, "change": 0, "destroy": 0}, "error": ""},
    )


def _bundle_role(app, run: dict[str, Any]) -> str:
    """The role ARN the runner is told to assume for `run`."""
    with TestClient(app, headers={"Authorization": f"Bearer {run['run_token']}"}) as runner:
        response = runner.get(f"{RUNS}/{run['run_id']}/bundle")
    assert response.status_code == 200, response.text
    return str(response.json()["run_role"]["role_arn"])


def test_staging_keeps_the_current_role(auth_client, workspace):
    """A staged role is stored beside the current one, which stays in effect."""
    body = _stage(auth_client, workspace["workspace_id"])
    assert body["run_role_arn"] == CURRENT
    assert body["pending_run_role_arn"] == STAGED


def test_staging_on_a_workspace_without_a_role_takes_it_at_once(auth_client):
    """With nothing working to protect, the role is simply saved."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    created = auth_client.post(BASE, json=payload).json()
    body = _stage(auth_client, created["workspace_id"])
    assert body["run_role_arn"] == STAGED
    assert body["pending_run_role_arn"] is None


def test_a_null_discards_the_staged_role(auth_client, workspace):
    """Keeping the current role is a merge patch null on the staged one."""
    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    body = _stage(auth_client, workspace_id, None)
    assert body["run_role_arn"] == CURRENT
    assert body["pending_run_role_arn"] is None


def test_setting_the_role_directly_discards_the_staged_one(auth_client, workspace):
    """A role typed in by hand switches at once, so nothing stays staged behind it."""
    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    other = "arn:aws:iam::333333333333:role/webbpulse-terraform-test-workspace-manual"
    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": other})
    assert response.json()["run_role_arn"] == other
    assert response.json()["pending_run_role_arn"] is None


def test_ordinary_runs_keep_the_current_role(app, auth_client, workspace, uploaded_config_version, state_machine):
    """A staged role no run proved never reaches a normal run or its bundle."""
    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    run = _start_run(auth_client, workspace_id, uploaded_config_version["config_version_id"]).json()
    assert run["run_role_arn"] == CURRENT
    assert run["run_role_check"] is False
    assert _bundle_role(app, run) == CURRENT


def test_a_role_check_run_assumes_the_staged_role(app, auth_client, workspace, uploaded_config_version, state_machine):
    """The verification run is plan only and is told to assume the staged role."""
    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    response = _start_run(auth_client, workspace_id, uploaded_config_version["config_version_id"], run_role_check=True)
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["run_role_check"] is True
    assert run["plan_only"] is True
    assert run["run_role_arn"] == STAGED
    assert _bundle_role(app, run) == STAGED


def test_a_role_check_without_a_staged_role_is_409(auth_client, workspace, uploaded_config_version, state_machine):
    """There is nothing to verify, and the code says so."""
    response = _start_run(
        auth_client, workspace["workspace_id"], uploaded_config_version["config_version_id"], run_role_check=True
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "PENDING_RUN_ROLE_MISSING"


def test_a_role_check_cannot_destroy(auth_client, workspace, uploaded_config_version, state_machine):
    """A verification run is a plan only run, so a destroy request is refused."""
    _stage(auth_client, workspace["workspace_id"])
    response = _start_run(
        auth_client,
        workspace["workspace_id"],
        uploaded_config_version["config_version_id"],
        run_role_check=True,
        is_destroy=True,
    )
    assert response.status_code == 422


def test_the_check_reports_the_staged_role_separately(auth_client, workspace):
    """The current role's verdict and the staged role's verdict are answered apart."""
    workspace_id = workspace["workspace_id"]
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["pending"] is None
    _stage(auth_client, workspace_id)
    pending = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["pending"]
    assert pending["role_arn"] == STAGED
    assert pending["status"] == "unverified"
    assert pending["error"] == run_role_check.PENDING_UNVERIFIED_MESSAGE


def test_a_proven_role_is_switched_to_by_the_recording_check(
    auth_client, workspace, uploaded_config_version, state_machine
):
    """Once the verification plan assumed the role, the POST check makes it current."""
    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    run = _start_run(
        auth_client, workspace_id, uploaded_config_version["config_version_id"], run_role_check=True
    ).json()
    _finish_plan(run["run_id"])

    read = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    assert read["pending"]["status"] == "connected"
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["run_role_arn"] == CURRENT

    body = auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["status"] == "connected"
    assert body["account_id"] == STAGED_ACCOUNT
    assert body["run_id"] == run["run_id"]
    assert body["pending"] is None
    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_arn"] == STAGED
    assert stored["pending_run_role_arn"] is None
    assert stored["run_role_account_id"] == STAGED_ACCOUNT


@pytest.mark.parametrize(
    ("status", "error"),
    [("errored", "The run failed with AssumeRoleFailed."), ("cancelled", None)],
)
def test_an_unproven_role_is_never_switched_to(auth_client, workspace, settings, status, error):
    """A refused or unfinished verification leaves the working role in place."""
    from webbpulse.dynamodb import new_ulid

    from app.common.db import repositories
    from app.common.db.tables import RUNS_COLLECTION

    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    item: dict[str, Any] = {
        "run_id": f"run-{new_ulid()}",
        "workspace_id": workspace_id,
        "config_version_id": "cv-seeded",
        "collection": RUNS_COLLECTION,
        "status": status,
        "run_role_arn": STAGED,
        "run_role_check": True,
        "created_at": "2026-09-26T00:00:00.000000+00:00",
    }
    if error is not None:
        item["error"] = error
    repositories.runs(settings).put(item)

    body = auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["pending"]["connected"] is False
    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_arn"] == CURRENT
    assert stored["pending_run_role_arn"] == STAGED


def test_a_restaged_role_is_not_switched_to_on_an_older_proof(auth_client, workspace, settings, monkeypatch):
    """The switch is conditional on the role still being the staged one."""
    workspace_id = workspace["workspace_id"]
    _stage(auth_client, workspace_id)
    other = "arn:aws:iam::444444444444:role/webbpulse-terraform-test-workspace-later"
    original = run_role_check.probe_run_role

    def restaged_meanwhile(workspace_id: str, *, settings: Any = None) -> dict[str, Any]:
        """Answer as if the staged role proved out, then stage another before the switch."""
        outcome = original(workspace_id, settings=settings)
        _stage(auth_client, workspace_id, other)
        pending = {**outcome["pending"], "role_arn": STAGED, "connected": True, "status": "connected"}
        return outcome | {"pending": pending}

    monkeypatch.setattr(run_role_check, "probe_run_role", restaged_meanwhile)
    run_role_check.check_run_role(workspace_id, settings=settings)
    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_arn"] == CURRENT
    assert stored["pending_run_role_arn"] == other
