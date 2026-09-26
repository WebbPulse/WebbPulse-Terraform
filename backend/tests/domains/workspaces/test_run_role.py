"""The run role: optional at create, its setup on every response, and the check.

The check never calls STS. It answers from the runs the runner already tried the
role in, so these tests seed runs in the states the state machine and the phase
result leave behind, and the end to end cases drive a real run through the API.
"""

from typing import Any

import pytest
from webbpulse.dynamodb import new_ulid

from app.common.db import repositories
from app.common.db.tables import RUNS_COLLECTION
from app.domains.runs import service as runs_service
from app.domains.workspaces import run_role_check, service
from tests.conftest import RUN_ROLE_NAME_PREFIX, RUNNER_TASK_ROLE_ARNS, WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"

ROLE_ARN = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-workspace-other"
ROLE_ACCOUNT = "870550636948"
CHECK_KEYS = {"connected", "status", "account_id", "error", "run_id", "checked_at"}


@pytest.fixture
def seed_run(settings):
    """A factory storing one run in the state a finished or failed run is left in."""
    counter = iter(range(1000))

    def seed(
        workspace_id: str,
        *,
        status: str,
        error: str | None = None,
        role_arn: str | None = WORKSPACE_PAYLOAD["run_role_arn"],
    ) -> str:
        """Store the run and return its id, each newer than the one before."""
        run_id = f"run-{new_ulid()}"
        created_at = f"2026-09-25T00:00:{next(counter):02d}.000000+00:00"
        item: dict[str, Any] = {
            "run_id": run_id,
            "workspace_id": workspace_id,
            "config_version_id": "cv-seeded",
            "collection": RUNS_COLLECTION,
            "status": status,
            "created_at": created_at,
            "updated_at": created_at,
            "finished_at": created_at,
        }
        if error is not None:
            item["error"] = error
        if role_arn is not None:
            item["run_role_arn"] = role_arn
        repositories.runs(settings).put(item)
        return run_id

    return seed


def _create(auth_client, **overrides: Any) -> dict[str, Any]:
    """Create a workspace from the shared payload with `overrides` applied."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_without_a_run_role_is_accepted(auth_client):
    """The role cannot exist before the workspace, so create does not demand one."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    response = auth_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["run_role_arn"] is None


def test_create_with_an_explicit_null_run_role_is_accepted(auth_client):
    """A client that sends the field as null is treated as one that omitted it."""
    body = _create(auth_client, run_role_arn=None)
    assert body["run_role_arn"] is None


def test_create_still_rejects_a_too_short_run_role(auth_client):
    """The ARN format check survives the field becoming optional."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "run_role_arn": "arn:aws"})
    assert response.status_code == 422


def test_every_workspace_response_carries_the_run_role_setup(auth_client, workspace):
    """The three values needed to build the role ride on the create response."""
    setup = workspace["run_role_setup"]
    assert setup["principal_arns"] == RUNNER_TASK_ROLE_ARNS
    assert setup["principal_arn"] == RUNNER_TASK_ROLE_ARNS[0]
    assert setup["external_id"] == workspace["workspace_id"]
    assert setup["role_name"] == RUN_ROLE_NAME_PREFIX + workspace["workspace_id"].removeprefix("ws-")


def test_the_get_and_list_responses_carry_the_setup_too(auth_client, workspace):
    """A client that reloads a workspace sees the same setup the create returned."""
    fetched = auth_client.get(f"{BASE}/{workspace['workspace_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["run_role_setup"] == workspace["run_role_setup"]

    listed = auth_client.get(BASE)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["run_role_setup"] == workspace["run_role_setup"]


def test_the_derived_role_name_fits_inside_the_iam_ceiling(workspace, settings):
    """A name past sixty four characters could not be created at all."""
    name = service.run_role_name(workspace["workspace_id"], settings=settings)
    assert len(name) <= service.IAM_ROLE_NAME_MAX_LENGTH


def test_the_derived_role_name_fits_for_the_longest_stack_prefix(settings, monkeypatch):
    """The production prefix plus a ULID is the longest name the stack can derive."""
    monkeypatch.setattr(settings, "RUN_ROLE_NAME_PREFIX", "webbpulse-terraform-staging-workspace-")
    name = service.run_role_name("ws-01JBQ0000000000000000000AA", settings=settings)
    assert len(name) <= service.IAM_ROLE_NAME_MAX_LENGTH


def test_a_new_workspace_has_never_been_checked(workspace):
    """Nothing claims a connection before one was made."""
    assert workspace["run_role_checked_at"] is None
    assert workspace["run_role_account_id"] is None


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_role_no_run_has_tried_is_unverified(auth_client, workspace, method):
    """Without a run there is nothing to answer from, so the answer says so."""
    response = getattr(auth_client, method)(f"{BASE}/{workspace['workspace_id']}/run-role/check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is False
    assert body["status"] == "unverified"
    assert body["error"] == run_role_check.RUN_ROLE_UNVERIFIED_MESSAGE
    assert body["run_id"] is None
    assert body["checked_at"] is None


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("planned_and_finished", None),
        ("awaiting_confirmation", None),
        ("applied", None),
        ("discarded", None),
        ("errored", "The run failed with PlanFailed."),
        ("errored", "The run failed with InitFailed."),
        ("errored", "The apply phase exited 1."),
    ],
)
def test_a_run_past_assume_role_proves_the_connection(auth_client, workspace, seed_run, status, error):
    """Every state only reachable after the runner assumed the role reads as connected."""
    workspace_id = workspace["workspace_id"]
    run_id = seed_run(workspace_id, status=status, error=error)
    body = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["connected"] is True
    assert body["status"] == "connected"
    assert body["account_id"] == ROLE_ACCOUNT
    assert body["error"] is None
    assert body["run_id"] == run_id
    assert body["checked_at"]


def test_a_refused_assume_role_reads_as_a_trust_problem(auth_client, workspace, seed_run):
    """STS will not say which half failed, so the message names both."""
    workspace_id = workspace["workspace_id"]
    run_id = seed_run(workspace_id, status="errored", error="The run failed with AssumeRoleFailed.")
    body = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["connected"] is False
    assert body["status"] == "failed"
    assert body["account_id"] is None
    assert body["error"] == run_role_check.RUN_ROLE_ASSUME_FAILED_MESSAGE
    assert body["run_id"] == run_id


@pytest.mark.parametrize(
    ("status", "error"),
    [
        ("pending", None),
        ("planning", None),
        ("cancelled", None),
        ("errored", "The run failed with BundleFetchFailed."),
        ("errored", "The run failed with ConfigDownloadFailed."),
        ("errored", "The run failed with States.Timeout."),
    ],
)
def test_a_run_that_ended_before_assume_role_proves_nothing(auth_client, workspace, seed_run, status, error):
    """A run that never reached AssumeRole says nothing about the role."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status=status, error=error)
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["status"] == "unverified"


def test_the_newest_verdict_wins(auth_client, workspace, seed_run):
    """A trust policy fixed after a refusal reads as connected once a run proves it."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="errored", error="The run failed with AssumeRoleFailed.")
    latest = seed_run(workspace_id, status="planned_and_finished")
    seed_run(workspace_id, status="cancelled")
    body = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["status"] == "connected"
    assert body["run_id"] == latest


def test_a_broken_trust_after_a_success_reads_as_failed(auth_client, workspace, seed_run):
    """A stale success must not outlive the trust policy that earned it."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    seed_run(workspace_id, status="errored", error="The run failed with AssumeRoleFailed.")
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["status"] == "failed"


def test_runs_against_another_role_are_ignored(auth_client, workspace, seed_run):
    """A success earned by the previous role says nothing about the current one."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied", role_arn=ROLE_ARN)
    seed_run(workspace_id, status="applied", role_arn=None)
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["status"] == "unverified"


def test_runs_on_another_workspace_are_ignored(auth_client, workspace, seed_run):
    """Evidence is read per workspace, never across them."""
    other = _create(auth_client, name="other")
    seed_run(other["workspace_id"], status="applied")
    assert auth_client.get(f"{BASE}/{workspace['workspace_id']}/run-role/check").json()["status"] == "unverified"


def test_the_check_answers_the_same_keys_either_way(auth_client, workspace, seed_run):
    """Nothing but the verdict and where it came from rides on the response."""
    workspace_id = workspace["workspace_id"]
    assert set(auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()) == CHECK_KEYS
    seed_run(workspace_id, status="applied")
    assert set(auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()) == CHECK_KEYS


def test_the_check_never_calls_sts(auth_client, workspace, seed_run, monkeypatch):
    """The API holds no path into the account the role lives in."""
    import boto3

    real_client: Any = boto3.client

    def refuse_sts(name: str, *args: Any, **kwargs: Any) -> Any:
        """Fail the test on any STS client, passing every other service through."""
        assert name != "sts", "the run role check must not call STS"
        return real_client(name, *args, **kwargs)

    monkeypatch.setattr(boto3, "client", refuse_sts)
    seed_run(workspace["workspace_id"], status="applied")
    assert auth_client.post(f"{BASE}/{workspace['workspace_id']}/run-role/check").json()["connected"] is True


def test_the_check_records_the_account_the_run_proved(auth_client, workspace, seed_run):
    """A proven role stamps the row with the account and the moment of the proof."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="planned_and_finished")
    body = auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["connected"] is True

    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_account_id"] == ROLE_ACCOUNT
    assert stored["run_role_checked_at"]


def test_a_failed_check_clears_an_earlier_success(auth_client, workspace, seed_run):
    """The recorded success goes as soon as a newer run is refused."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True

    seed_run(workspace_id, status="errored", error="The run failed with AssumeRoleFailed.")
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is False

    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_checked_at"] is None
    assert stored["run_role_account_id"] is None


def test_the_check_is_400_when_no_role_is_configured(auth_client):
    """A workspace with nothing to assume is a request error, not a failed check."""
    created = _create(auth_client, run_role_arn=None)
    for method in ("get", "post"):
        response = getattr(auth_client, method)(f"{BASE}/{created['workspace_id']}/run-role/check")
        assert response.status_code == 400, response.text
        assert response.json()["error_code"] == "RUN_ROLE_MISSING"


def test_the_check_is_404_for_an_absent_workspace(auth_client):
    """A well formed id that names nothing is a 404."""
    for method in ("get", "post"):
        response = getattr(auth_client, method)(f"{BASE}/ws-01JBQ0000000000000000000AA/run-role/check")
        assert response.status_code == 404


def test_the_recording_check_needs_the_write_scope(scoped_client):
    """The POST writes to the row, so reading a workspace is not enough."""
    with scoped_client("workspaces:read") as client:
        response = client.post(f"{BASE}/ws-01JBQ0000000000000000000AA/run-role/check")
    assert response.status_code == 403


def test_the_read_only_check_needs_only_the_read_scope(scoped_client, workspace):
    """Nothing is written, so a read-only caller may look."""
    with scoped_client("workspaces:read") as client:
        response = client.get(f"{BASE}/{workspace['workspace_id']}/run-role/check")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unverified"


def test_the_read_only_check_stamps_nothing(auth_client, workspace, seed_run):
    """A provider reading on every plan must not write to the row."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True

    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_checked_at"] is None
    assert stored["run_role_account_id"] is None
    assert stored["updated_at"] == workspace["updated_at"]


def test_the_read_only_check_leaves_an_earlier_success_alone(auth_client, workspace, seed_run):
    """A read that fails does not clear what the POST recorded."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True

    seed_run(workspace_id, status="errored", error="The run failed with AssumeRoleFailed.")
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is False

    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_account_id"] == ROLE_ACCOUNT


def test_the_read_only_check_is_repeatable(auth_client, workspace, seed_run):
    """Same inputs, same answer, no drift between two reads."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    first = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    second = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    assert first == second


def test_a_patched_run_role_clears_the_check(auth_client, workspace, seed_run):
    """The previous success belonged to the previous role."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": ROLE_ARN})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_role_arn"] == ROLE_ARN
    assert body["run_role_checked_at"] is None
    assert body["run_role_account_id"] is None
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["status"] == "unverified"


def test_an_unrelated_patch_keeps_the_check(auth_client, workspace, seed_run):
    """Editing the description says nothing about the role."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    auth_client.post(f"{BASE}/{workspace_id}/run-role/check")

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"description": "Edited."})
    assert response.status_code == 200, response.text
    assert response.json()["run_role_account_id"] == ROLE_ACCOUNT


def test_patching_the_same_run_role_keeps_the_check(auth_client, workspace, seed_run):
    """A no-op write is not a reason to forget a connection that still holds."""
    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, status="applied")
    auth_client.post(f"{BASE}/{workspace_id}/run-role/check")

    response = auth_client.patch(
        f"{BASE}/{workspace_id}",
        json={"run_role_arn": WORKSPACE_PAYLOAD["run_role_arn"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_account_id"] == ROLE_ACCOUNT


def test_a_created_run_keeps_the_role_it_was_created_with(created_run):
    """Evidence is matched on the role a run carried, so every run stores it."""
    stored = runs_service.get_run(created_run["run_id"])
    assert stored["run_role_arn"] == WORKSPACE_PAYLOAD["run_role_arn"]


def test_a_real_plan_only_run_is_the_check(auth_client, plan_only_run):
    """A plan the runner finished proves the role end to end through the API."""
    workspace_id = plan_only_run["workspace_id"]
    assert auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()["status"] == "unverified"

    runs_service.record_phase_result(
        plan_only_run["run_id"],
        {"phase": "plan", "exit_code": 0, "changes": {"add": 0, "change": 0, "destroy": 0}, "error": ""},
    )
    body = auth_client.get(f"{BASE}/{workspace_id}/run-role/check").json()
    assert body["status"] == "connected"
    assert body["run_id"] == plan_only_run["run_id"]


def test_the_setup_names_every_runner_task_role(workspace):
    """A trust policy naming only one phase's role would break the other phase."""
    assert len(workspace["run_role_setup"]["principal_arns"]) == len(RUNNER_TASK_ROLE_ARNS)


def test_a_run_role_can_be_attached_after_the_fact(auth_client):
    """The setup the create returned is followed by a patch carrying the ARN."""
    created = _create(auth_client, run_role_arn=None)
    response = auth_client.patch(
        f"{BASE}/{created['workspace_id']}",
        json={"run_role_arn": ROLE_ARN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] == ROLE_ARN
