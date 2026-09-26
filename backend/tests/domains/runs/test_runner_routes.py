"""The three routes the runner owns, and the token that gates them.

The bundle carries decrypted sensitive variables and, on an apply, an
unrestricted session policy, so the gate is the security boundary of this domain
and is tested from every angle a caller could come at it.
"""

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import RUNNER_SCOPE
from app.domains.runs import service as runs_service
from tests.conftest import STATE_KMS_KEY_ARN, mint_key

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


def test_the_bundle_says_whether_the_plan_destroys(runner_client, created_run):
    """An ordinary run's bundle carries `is_destroy` false."""
    body = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()
    assert body["is_destroy"] is False


def test_a_destroy_runs_bundle_asks_for_a_destroy_plan(
    app, auth_client, workspace, uploaded_config_version, state_machine
):
    """The runner learns to plan `-destroy` from the bundle alone."""
    created = auth_client.post(
        BASE,
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "is_destroy": True,
        },
    ).json()
    with TestClient(app, headers={"Authorization": f"Bearer {created['run_token']}"}) as runner:
        body = runner.get(f"{BASE}/{created['run_id']}/bundle").json()
    assert body["is_destroy"] is True
    assert body["phase"] == "plan"


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
    assert backend["kms_key_id"] == STATE_KMS_KEY_ARN


def test_the_bundle_refuses_to_serve_an_empty_kms_key(created_run, settings, monkeypatch):
    """An unset `STATE_KMS_KEY_ARN` fails here rather than at `terraform init`.

    The runner copies the value into the backend override verbatim, and terraform
    rejects an empty `kms_key_id`, so a deployment that never set the variable
    would otherwise only surface as a failed init inside every run.
    """
    monkeypatch.setattr(settings, "STATE_KMS_KEY_ARN", "")
    with pytest.raises(runs_service.StateKmsKeyMissing):
        runs_service.run_bundle(created_run["run_id"], settings=settings)


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


def test_the_bundle_carries_only_the_read_direction(runner_client, created_run):
    """The bundle mints the plan download and no upload.

    An upload's URL signs the exact `Content-Length` the client will send, which
    is unknown when the bundle is built, so a put url here could only be signed
    for a ceiling and S3 would refuse every real PUT against it.
    """
    artifacts = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()["artifacts"]
    assert artifacts["plan_get_url"].startswith("https://")
    assert set(artifacts) == {"plan_get_url"}


def test_an_artifact_upload_is_signed_for_the_declared_size(runner_client, created_run):
    """The route hands back a URL and the headers its signature requires."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": "plan", "size_bytes": 4096},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url"].startswith("https://")
    assert f"runs/{created_run['run_id']}/plan.tfplan" in body["url"]
    assert body["headers"]["Content-Type"] == "application/octet-stream"
    assert body["headers"]["Content-Length"] == "4096"
    assert body["expires_in"] == runs_service.ARTIFACT_URL_TTL


@pytest.mark.parametrize(
    ("artifact", "key", "content_type"),
    [
        ("plan", "plan.tfplan", "application/octet-stream"),
        ("plan_json", "plan.json", "application/json"),
        ("log", "plan.log", "text/plain"),
    ],
)
def test_each_artifact_kind_has_its_own_key_and_type(runner_client, created_run, artifact, key, content_type):
    """The three kinds sign three different keys and three different content types."""
    body = runner_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": artifact, "size_bytes": 128},
    ).json()
    assert f"runs/{created_run['run_id']}/{key}" in body["url"]
    assert body["headers"]["Content-Type"] == content_type


def test_the_log_upload_key_is_per_phase(auth_client, runner_client, created_run, awaiting_confirmation):
    """A plan and an apply upload to different keys, so neither overwrites the other.

    Both transcripts have to survive, because the plan's output is the evidence
    the apply was confirmed against. The phase comes from the run's status, so a
    plan-phase runner cannot ask for the apply key.
    """
    plan = runner_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": "log", "size_bytes": 64},
    ).json()
    assert f"runs/{created_run['run_id']}/plan.log" in plan["url"]

    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    apply = runner_client.post(
        f"{BASE}/{run_id}/artifact-uploads",
        json={"artifact": "log", "size_bytes": 64},
    ).json()
    assert f"runs/{run_id}/apply.log" in apply["url"]


@pytest.mark.parametrize(
    ("artifact", "size_bytes"),
    [("plan", 500_000_001), ("plan_json", 500_000_001), ("log", 50_000_001)],
)
def test_an_upload_above_the_ceiling_is_413(runner_client, created_run, artifact, size_bytes):
    """A size past the artifact's ceiling is refused rather than signed.

    The ceiling is the only bound on what a signed URL can park in the bucket,
    since S3 enforces the declared length and nothing else.
    """
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": artifact, "size_bytes": size_bytes},
    )
    assert response.status_code == 413
    assert str(size_bytes) in response.json()["message"]


def test_a_zero_size_upload_is_422(runner_client, created_run):
    """Nothing to upload is not an upload, and `presigned_put` refuses it anyway."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": "plan", "size_bytes": 0},
    )
    assert response.status_code == 422


def test_an_unknown_artifact_kind_is_422(runner_client, created_run):
    """Only the three kinds the runner uploads are accepted."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": "state", "size_bytes": 128},
    )
    assert response.status_code == 422


def test_an_artifact_upload_needs_a_token(client, created_run):
    """No credential at all is a 401, not a signed URL into the artifacts bucket."""
    response = client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": "plan", "size_bytes": 128},
    )
    assert response.status_code == 401


def test_a_human_scope_cannot_mint_an_artifact_upload(auth_client, created_run):
    """A person's key is not a run token here either.

    A signed PUT over a run's plan key would let a caller replace the plan an
    apply is about to run, so the gate is the run's own token.
    """
    response = auth_client.post(
        f"{BASE}/{created_run['run_id']}/artifact-uploads",
        json={"artifact": "plan", "size_bytes": 128},
    )
    assert response.status_code == 401


def test_a_token_cannot_mint_an_upload_for_another_run(runner_client):
    """A token refuses an id that is not its own run, present or not."""
    response = runner_client.post(
        f"{BASE}/run-01JBQ0000000000000000000AA/artifact-uploads",
        json={"artifact": "plan", "size_bytes": 128},
    )
    assert response.status_code == 401


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


def test_the_runner_reports_the_payload_it_actually_sends(runner_client, created_run):
    """The runner's literal payload is accepted, null error and extra fields and all.

    The runner posts its own `PhaseResult`, which carries `run_id` and
    `has_changes` that the route ignores and, on a clean phase, `error` as null.
    A schema that rejected null ended every successful plan as a 422 and so as
    an errored run.
    """
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={
            "run_id": created_run["run_id"],
            "phase": "plan",
            "exit_code": 0,
            "changes": {"add": 1, "change": 0, "destroy": 0},
            "has_changes": True,
            "error": None,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_confirmation"
    assert runs_service.get_run(created_run["run_id"]).get("error", "") == ""


def test_a_null_error_does_not_become_the_string_none(runner_client, created_run):
    """A failed phase reporting a null error errors on the exit code, not on "None"."""
    response = runner_client.post(
        f"{BASE}/{created_run['run_id']}/phase-result",
        json={"phase": "plan", "exit_code": 1, "changes": {}, "error": None},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "errored"
    assert "None" not in runs_service.get_run(created_run["run_id"])["error"]


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
