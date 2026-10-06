"""Auto-apply: a workspace whose successful plans apply without a person.

The confirmation is made when the state machine's task token lands, so every case
drives the confirmations consumer with a run that planned with changes.
"""

import json
import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import ADMIN, ALL_SCOPES
from app.domains.runs import reporting
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import confirmations
from tests.conftest import person_headers, seed_user

TASK_TOKEN = "auto-apply-task-token"
PERSON = "user-auto-apply"


class RecordingStepFunctions:
    """Records confirmations, or refuses them when told to, and passes every other call through."""

    def __init__(self, factory: Any) -> None:
        self.factory = factory
        self.client: Any = None
        self.successes: list[dict[str, Any]] = []
        self.refuse = False

    def bind(self, settings: Any) -> "RecordingStepFunctions":
        """Stand in for the client factory, keeping the real client for everything else."""
        self.client = self.factory(settings)
        return self

    def __getattr__(self, name: str) -> Any:
        """Every call but the confirmation goes to the moto backed client."""
        return getattr(self.client, name)

    def send_task_success(self, **kwargs: Any) -> None:
        """Record a success, or raise as Step Functions would on a dead token."""
        if self.refuse:
            raise RuntimeError("Step Functions refused the token.")
        self.successes.append(kwargs)


@pytest.fixture
def stepfunctions(monkeypatch):
    """The confirmation's `SendTaskSuccess`, recorded instead of sent."""
    recorder = RecordingStepFunctions(runs_service._stepfunctions)
    monkeypatch.setattr(runs_service, "_stepfunctions", recorder.bind)
    return recorder


def _record(run_id: str) -> dict[str, Any]:
    """The queue record the state machine sends once the plan reported."""
    body = {"kind": confirmations.CONFIRMATION_KIND, "run_id": run_id, "task_token": TASK_TOKEN}
    return {"messageId": f"msg-{run_id}", "body": json.dumps(body)}


def _set_auto_apply(auth_client: TestClient, workspace: dict[str, Any], value: bool) -> None:
    """Turn the workspace's auto-apply on or off as an admin."""
    response = auth_client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": value})
    assert response.status_code == 200, response.text


def _create(auth_client: TestClient, workspace: dict[str, Any], config_version: dict[str, Any], **extra: Any):
    """Create a run on the workspace and return it."""
    response = auth_client.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": config_version["config_version_id"],
        }
        | extra,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _plan(run_id: str, *, exit_code: int = 0, add: int = 1) -> dict[str, Any]:
    """Report the plan phase as the runner would."""
    return runs_service.record_phase_result(
        run_id,
        {"phase": "plan", "exit_code": exit_code, "changes": {"add": add, "change": 0, "destroy": 0}, "error": ""},
    )


@pytest.fixture
def auto_workspace(auth_client, workspace, uploaded_config_version, state_machine):
    """A workspace with auto-apply on and an uploaded config version."""
    _set_auto_apply(auth_client, workspace, True)
    return workspace


def test_a_workspace_does_not_auto_apply_by_default(workspace):
    """Like HCP Terraform, every plan waits for a confirmation until it is turned on."""
    assert workspace["auto_apply"] is False


def test_a_planned_run_is_confirmed_by_the_system(
    auth_client, auto_workspace, uploaded_config_version, stepfunctions, caplog
):
    """The token lands, the run applies and the decision names auto-apply, not a person."""
    run = _create(auth_client, auto_workspace, uploaded_config_version)
    assert run["auto_apply"] is True
    _plan(run["run_id"])

    with caplog.at_level(logging.INFO):
        confirmations.handle_record(_record(run["run_id"]))

    stored = runs_service.get_run(run["run_id"])
    assert stored["status"] == "applying"
    assert stored["decision"]["action"] == "confirmed"
    assert stored["decision"]["actor"] == {"kind": "system", "id": "auto-apply", "display_name": "Auto-apply"}
    assert [call["taskToken"] for call in stepfunctions.successes] == [TASK_TOKEN]
    assert any(getattr(record, "event", "") == runs_service.AUTO_APPLY_EVENT for record in caplog.records)

    rendered = auth_client.get(f"/api/v1/runs/{run['run_id']}").json()
    assert rendered["decision"]["actor"]["kind"] == "system"
    assert rendered["auto_apply"] is True


def test_a_workspace_without_auto_apply_waits(
    auth_client, workspace, uploaded_config_version, state_machine, stepfunctions
):
    """The token lands and the run stays for a person."""
    run = _create(auth_client, workspace, uploaded_config_version)
    assert run["auto_apply"] is False
    _plan(run["run_id"])

    confirmations.handle_record(_record(run["run_id"]))

    assert runs_service.get_run(run["run_id"])["status"] == "awaiting_confirmation"
    assert stepfunctions.successes == []


def test_a_plan_only_run_never_auto_applies(auth_client, auto_workspace, uploaded_config_version, stepfunctions):
    """A speculative plan finishes at its plan, whatever the workspace says."""
    run = _create(auth_client, auto_workspace, uploaded_config_version, plan_only=True)
    assert run["auto_apply"] is False

    assert _plan(run["run_id"])["status"] == "planned_and_finished"
    assert stepfunctions.successes == []


def test_a_pull_request_run_is_never_marked_to_auto_apply(auto_workspace, uploaded_config_version, state_machine):
    """A `vcs_pr` run is not created to auto-apply, even one that somehow is not plan only."""
    run = runs_service.create_run(
        {
            "workspace_id": auto_workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
        },
        actor=None,
        source="vcs_pr",
    )

    assert run.get("auto_apply") is False
    assert runs_service.auto_apply_eligible(run | {"status": "awaiting_confirmation", "auto_apply": True}) is False


def test_a_push_run_auto_applies(auto_workspace, uploaded_config_version, state_machine, stepfunctions):
    """A push to the tracked branch is a normal run and applies."""
    run = runs_service.create_run(
        {
            "workspace_id": auto_workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
        },
        actor=None,
        source="vcs_push",
    )
    _plan(run["run_id"])

    confirmations.handle_record(_record(run["run_id"]))

    assert runs_service.get_run(run["run_id"])["status"] == "applying"


def test_an_errored_plan_never_applies(auth_client, auto_workspace, uploaded_config_version, stepfunctions):
    """A failed plan ends the run, and a late token cannot revive it."""
    run = _create(auth_client, auto_workspace, uploaded_config_version)

    assert _plan(run["run_id"], exit_code=1)["status"] == "errored"
    with pytest.raises(runs_service.RunNotFound):
        confirmations.handle_record(_record(run["run_id"]))
    assert stepfunctions.successes == []


def test_a_plan_without_changes_finishes(auth_client, auto_workspace, uploaded_config_version, stepfunctions):
    """Nothing to apply means nothing is confirmed."""
    run = _create(auth_client, auto_workspace, uploaded_config_version)

    assert _plan(run["run_id"], add=0)["status"] == "planned_and_finished"
    assert stepfunctions.successes == []


def test_turning_it_off_mid_run_stops_the_apply(auth_client, auto_workspace, uploaded_config_version, stepfunctions):
    """The workspace is read again when the token lands, so turning it off holds the run."""
    run = _create(auth_client, auto_workspace, uploaded_config_version)
    _plan(run["run_id"])
    _set_auto_apply(auth_client, auto_workspace, False)

    confirmations.handle_record(_record(run["run_id"]))

    stored = runs_service.get_run(run["run_id"])
    assert stored["status"] == "awaiting_confirmation"
    assert stored["auto_apply"] is False
    assert stepfunctions.successes == []


def test_a_refused_confirmation_leaves_the_run_for_a_person(
    auth_client, auto_workspace, uploaded_config_version, stepfunctions
):
    """The token goes back on the run and a person can still confirm it."""
    run = _create(auth_client, auto_workspace, uploaded_config_version)
    _plan(run["run_id"])
    stepfunctions.refuse = True

    confirmations.handle_record(_record(run["run_id"]))

    stored = runs_service.get_run(run["run_id"])
    assert stored["status"] == "awaiting_confirmation"
    assert stored["confirm_task_token"] == TASK_TOKEN
    assert stored["auto_apply"] is False
    assert not stored.get("decision")

    stepfunctions.refuse = False
    response = auth_client.post(f"/api/v1/runs/{run['run_id']}/confirm")
    assert response.status_code == 200, response.text


def test_a_held_auto_apply_run_reports_in_progress():
    """A run about to auto-apply is not shown on GitHub as needing a person."""
    state = reporting.check_state({"status": "awaiting_confirmation", "auto_apply": True})
    assert (state.status, state.conclusion) == ("in_progress", None)
    assert reporting.check_state({"status": "awaiting_confirmation"}).conclusion == "action_required"


def _person(app, *, admin: bool, auth_age: int = 0) -> TestClient:
    """A signed in person, with admin only when asked."""
    seed_user(PERSON, is_admin=admin)
    scopes = ALL_SCOPES if admin else tuple(scope for scope in ALL_SCOPES if scope != ADMIN)
    roles = ("admin",) if admin else ()
    return TestClient(app, headers=person_headers(user_id=PERSON, scopes=scopes, roles=roles, auth_age=auth_age))


def test_a_non_admin_cannot_turn_it_on(app, workspace):
    """Workspace write is not enough, since auto-apply turns run creation into applying."""
    with _person(app, admin=False) as client:
        response = client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": True})

    assert response.status_code == 403, response.text


def test_a_non_admin_key_cannot_turn_it_on(scoped_client, workspace):
    """An agent key without admin is refused the same way."""
    with scoped_client("workspaces:read", "workspaces:write") as client:
        response = client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": True})

    assert response.status_code == 403, response.text


def test_a_non_admin_may_resend_the_stored_value(app, workspace):
    """A full form save that leaves auto-apply alone is no change."""
    with _person(app, admin=False) as client:
        response = client.patch(
            f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": False, "description": "edited"}
        )

    assert response.status_code == 200, response.text


def test_a_non_admin_cannot_create_a_workspace_with_it_on(scoped_client):
    """The create is held to the same rule as the edit."""
    from tests.conftest import WORKSPACE_PAYLOAD

    with scoped_client("workspaces:read", "workspaces:write") as client:
        response = client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"auto_apply": True})

    assert response.status_code == 403, response.text


def test_turning_it_on_takes_the_step_up(app, workspace):
    """It stands in for the step-up gated confirmation, so a stale admin session is refused."""
    with _person(app, admin=True, auth_age=24 * 3600) as client:
        response = client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": True})

    assert response.status_code == 401, response.text


def test_an_admin_change_is_audited(app, workspace, caplog):
    """The change names who made it and both values."""
    with _person(app, admin=True) as client, caplog.at_level(logging.INFO):
        response = client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": True})

    assert response.status_code == 200, response.text
    assert response.json()["auto_apply"] is True
    audits = [record for record in caplog.records if getattr(record, "event", "") == "workspaces.workspace.auto_apply"]
    assert len(audits) == 1
    assert getattr(audits[0], "subject") == PERSON
    assert getattr(audits[0], "previous") is False
    assert getattr(audits[0], "auto_apply") is True


def test_null_is_not_a_value(auth_client, workspace):
    """Auto-apply is always on or off, never cleared."""
    response = auth_client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"auto_apply": None})

    assert response.status_code == 422, response.text
