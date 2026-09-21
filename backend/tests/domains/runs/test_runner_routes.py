"""The two routes the runner owns, and the token that gates them.

The bundle carries decrypted sensitive variables and, on an apply, an
unrestricted session policy, so the gate is the security boundary of this domain
and is tested from every angle a caller could come at it.
"""

from fastapi.testclient import TestClient

from app.common.core.auth import RUNNER_SCOPE
from app.domains.runs import service as runs_service
from tests.conftest import mint_key

BASE = "/api/v1/runs"


def test_the_bundle_needs_a_token(client, created_run):
    """No credential at all is a 401, not an anonymous bundle."""
    assert client.get(f"{BASE}/{created_run['run_id']}/bundle").status_code == 401


def test_a_human_scope_cannot_open_the_bundle(auth_client, created_run):
    """A person's key, however scoped, is not a run token.

    The bundle decrypts sensitive variables, so no human scope opens it. Only the
    run's own minted token does.
    """
    response = auth_client.get(f"{BASE}/{created_run['run_id']}/bundle")
    assert response.status_code == 401


def test_another_runs_token_cannot_open_this_bundle(
    app, auth_client, workspace, uploaded_config_version, state_machine, created_run
):
    """A run token is bound to its own run and no other.

    Without the binding check any runner token would be a master key to every
    run's variables, since all of them carry the same `runner` scope.
    """
    other = auth_client.post(
        BASE,
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": False,
        },
    ).json()
    runs_service.finish_run(created_run["run_id"], "applied")
    assert runs_service.get_run(other["run_id"])["status"] == "planning"
    token = mint_key(RUNNER_SCOPE, user_id=other["run_id"])

    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as runner:
        response = runner.get(f"{BASE}/{created_run['run_id']}/bundle")
    assert response.status_code == 401


def test_a_key_without_the_runner_scope_is_refused(app, created_run):
    """A `wpk_` key minted with other scopes cannot stand in for a run token."""
    token = mint_key("runs:read", "runs:write", "runs:apply")
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as caller:
        response = caller.get(f"{BASE}/{created_run['run_id']}/bundle")
    assert response.status_code == 401


def test_a_garbage_token_is_refused(app, created_run):
    """An unparseable bearer is a 401."""
    with TestClient(app, headers={"Authorization": "Bearer wpk_not-a-real-key"}) as caller:
        response = caller.get(f"{BASE}/{created_run['run_id']}/bundle")
    assert response.status_code == 401


def test_the_bundle_carries_the_engine_and_the_config(runner_client, created_run, workspace):
    """The runner gets what it needs to fetch and run the configuration."""
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()
    assert body["run_id"] == created_run["run_id"]
    assert body["engine"] == workspace["engine"]
    assert body["engine_version"] == workspace["engine_version"]
    assert body["config_url"].startswith("https://")
    assert body["run_role"]["role_arn"] == workspace["run_role_arn"]


def test_the_bundle_backend_points_at_the_workspace_state(runner_client, created_run, workspace):
    """The backend points at this workspace's state key under the KMS key.

    The runner writes these four values straight into the S3 backend override
    and always adds `use_lockfile = true`, which is the only thing stopping two
    runs from writing the same state.
    """
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()
    backend = body["backend"]
    assert backend["key"] == f"workspaces/{workspace['workspace_id']}/terraform.tfstate"
    assert backend["bucket"]
    assert backend["region"]
    assert "kms_key_id" in backend


def test_the_bundle_carries_decrypted_variables(auth_client, runner_client, created_run, workspace):
    """A sensitive value reaches the runner in the clear, split by category.

    This is the one place a sealed value is opened, and the reason the route is
    gated by a run token rather than a human scope.
    """
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        f"/api/v1/workspaces/{workspace_id}/variables/secret_token",
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    auth_client.put(
        f"/api/v1/workspaces/{workspace_id}/variables/region",
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()
    assert body["environment_variables"]["secret_token"] == "super-secret"
    assert body["terraform_variables"]["region"] == "us-west-2"


def test_the_bundle_carries_the_artifact_urls(runner_client, created_run):
    """The runner is handed every artifact URL it uses, in both directions."""
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()
    artifacts = body["artifacts"]
    assert artifacts["plan_put_url"].startswith("https://")
    assert artifacts["plan_json_put_url"].startswith("https://")
    assert artifacts["plan_get_url"].startswith("https://")
    assert artifacts["log_put_url"].startswith("https://")


def test_the_log_put_url_is_per_phase(auth_client, runner_client, created_run, awaiting_confirmation):
    """A plan and an apply upload to different keys, so neither overwrites the other.

    Both transcripts have to survive, because the plan's output is the evidence
    the apply was confirmed against.
    """
    plan_url = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()["artifacts"]["log_put_url"]
    assert f"runs/{created_run['run_id']}/plan.log" in plan_url

    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    apply_url = runner_client.get(f"{BASE}/{run_id}/bundle").json()["artifacts"]["log_put_url"]
    assert f"runs/{run_id}/apply.log" in apply_url


def test_the_bundle_run_role_binds_the_external_id_to_the_workspace(runner_client, created_run, workspace):
    """The external id is the workspace id, so one workspace's role is not another's.

    A run role trusted with that condition cannot be assumed by a run against a
    different workspace even if its arn leaks.
    """
    role = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()["run_role"]
    assert role["external_id"] == workspace["workspace_id"]
    assert role["duration_seconds"] == 3600


def test_the_plan_phase_gets_a_read_only_session_policy(runner_client, created_run):
    """A planning run's policy grants no write outside its own artifacts.

    A plan that could write is the whole risk being designed out here: the
    read-only policy is what makes an unreviewed plan safe to run.
    """
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()
    assert body["phase"] == "plan"
    statements = body["run_role"]["session_policy"]["Statement"]
    assert not any(statement.get("Action") == "*" for statement in statements)
    assert {statement["Sid"] for statement in statements} == {
        "StateAndLock",
        "PlanArtifacts",
        "StateEncryption",
    }


def test_the_plan_bundle_carries_the_managed_read_only_policy_arn(runner_client, created_run):
    """The bundle hands the runner the ReadOnlyAccess ARN to union in.

    The inline document grants no reads at all now, so a bundle that dropped
    this field would give a plan a session that cannot refresh state.
    """
    role = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()["run_role"]
    assert role["session_policy_arns"] == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]


def test_the_apply_bundle_carries_no_session_policy_arns(auth_client, runner_client, awaiting_confirmation):
    """An apply's session unions nothing, because its inline policy allows everything."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    role = runner_client.get(f"{BASE}/{run_id}/bundle").json()["run_role"]
    assert role["session_policy_arns"] == []


def test_the_apply_phase_gets_an_unrestricted_session_policy(auth_client, runner_client, awaiting_confirmation):
    """An applying run needs to make the changes its plan described."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    body = runner_client.get(f"{BASE}/{run_id}/bundle").json()
    assert body["phase"] == "apply"
    assert body["run_role"]["session_policy"]["Statement"][0]["Action"] == "*"


def test_the_phase_comes_from_the_status_not_the_caller(runner_client, created_run):
    """A plan-phase runner cannot request the apply policy by asking for it.

    Deriving the phase from the stored status closes the escalation where a
    compromised plan runner asks for the unrestricted policy.
    """
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle", params={"phase": "apply"}).json()
    assert body["phase"] == "plan"
    assert body["run_role"]["session_policy"]["Statement"][0].get("Action") != "*"


def test_a_token_cannot_open_a_bundle_for_another_id(runner_client):
    """A token refuses an id that is not its own run, present or not.

    The binding check runs before the lookup, so an absent run is a 401 rather
    than a 404: the gate does not confirm which run ids exist.
    """
    assert runner_client.get(f"{BASE}/run-01JBQ0000000000000000000AA/bundle").status_code == 401


def test_the_phase_result_needs_a_token(client, created_run):
    """Reporting a phase without a run token is a 401."""
    response = client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "plan", "exit_code": 0, "changes": {"add": 0, "change": 0, "destroy": 0}, "error": ""},
    )
    assert response.status_code == 401


def test_a_human_scope_cannot_report_a_phase(auth_client, created_run):
    """A person cannot report a plan result and skip the runner.

    Otherwise a caller with `runs:write` could report a no-change plan and walk
    the run to a terminal status without anything having run.
    """
    response = auth_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "plan", "exit_code": 0, "changes": {"add": 0, "change": 0, "destroy": 0}, "error": ""},
    )
    assert response.status_code == 401


def test_the_runner_reports_a_plan_result(runner_client, created_run):
    """The runner's own token advances the run."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "plan", "exit_code": 0, "changes": {"add": 1, "change": 0, "destroy": 0}, "error": ""},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == created_run["run_id"]
    assert body["status"] == "awaiting_confirmation"


def test_the_runner_reports_a_failure(runner_client, created_run):
    """A reported failure errors the run."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "plan", "exit_code": 1, "changes": {}, "error": "boom"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "errored"


def test_a_phase_result_for_the_wrong_phase_is_409(runner_client, created_run):
    """An apply result on a planning run is refused at the route."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "apply", "exit_code": 0, "changes": {}, "error": ""},
    )
    assert response.status_code == 409


def test_an_unknown_phase_is_422(runner_client, created_run):
    """Only the two contract phases are accepted."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "destroy", "exit_code": 0, "changes": {}, "error": ""},
    )
    assert response.status_code == 422
