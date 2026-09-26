"""Safe and force workspace delete, the HCP Terraform contract.

A safe delete refuses while the current state tracks any resource instance, a
force delete skips that check, and both refuse while a run is unfinished. The
refusals are the point: every one is asserted to leave the workspace, its state,
its variables and its runs exactly where they were.
"""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import ClientError

from app.common.db import repositories
from app.common.db.tables import SEMAPHORE_RUN_ID
from app.domains.workspaces.state_versions import state_key
from tests.conftest import REGION, STATE_BUCKET

BASE = "/api/v1/workspaces"


def enable_versioning() -> None:
    """Turn on object versioning for the state bucket, as the stack does."""
    boto3.client("s3", region_name=REGION).put_bucket_versioning(
        Bucket=STATE_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )


def write_state(workspace_id: str, body: bytes) -> None:
    """Put one current state object for the workspace."""
    boto3.client("s3", region_name=REGION).put_object(Bucket=STATE_BUCKET, Key=state_key(workspace_id), Body=body)


def state_with(*instance_counts: int) -> bytes:
    """A state body with one resource per count, each holding that many instances."""
    return json.dumps(
        {
            "version": 4,
            "serial": 3,
            "resources": [
                {
                    "mode": "managed",
                    "type": "random_pet",
                    "name": f"pet{index}",
                    "instances": [{"attributes": {"id": "secret-looking-value"}} for _ in range(count)],
                }
                for index, count in enumerate(instance_counts)
            ],
        }
    ).encode()


def state_exists(workspace_id: str) -> bool:
    """Whether a current state object reads back for the workspace."""
    try:
        boto3.client("s3", region_name=REGION).head_object(Bucket=STATE_BUCKET, Key=state_key(workspace_id))
    except ClientError:
        return False
    return True


def seed_run(workspace_id: str, run_id: str, status: str, created_at: str = "2026-09-25T00:00:00Z") -> None:
    """Write one run row straight into the table, as the runs domain would."""
    repositories.runs().put(
        {"run_id": run_id, "workspace_id": workspace_id, "status": status, "created_at": created_at}
    )


def run_exists(run_id: str) -> bool:
    """Whether a run row reads back."""
    return repositories.runs().get({"run_id": run_id}) is not None


def add_variable(auth_client, workspace_id: str) -> None:
    """Set one plain variable on the workspace."""
    response = auth_client.put(
        f"{BASE}/{workspace_id}/variables/region",
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    assert response.status_code in (200, 201), response.text


def assert_untouched(auth_client, workspace_id: str) -> None:
    """The workspace and its variable still read back after a refusal."""
    assert auth_client.get(f"{BASE}/{workspace_id}").status_code == 200
    assert [item["key"] for item in auth_client.get(f"{BASE}/{workspace_id}/variables").json()["items"]] == ["region"]


def test_safe_delete_refuses_while_state_tracks_an_instance(auth_client, workspace):
    """A state with one instance is a 409 carrying the code, and nothing is removed."""
    workspace_id = workspace["workspace_id"]
    add_variable(auth_client, workspace_id)
    write_state(workspace_id, state_with(0, 1))
    seed_run(workspace_id, "run-01JBQ0000000000000000000R1", "applied")
    response = auth_client.delete(f"{BASE}/{workspace_id}")
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "WORKSPACE_MANAGES_RESOURCES"
    assert "secret-looking-value" not in response.text
    assert_untouched(auth_client, workspace_id)
    assert state_exists(workspace_id)
    assert run_exists("run-01JBQ0000000000000000000R1")


def test_force_delete_removes_everything_and_leaves_a_delete_marker(auth_client, workspace):
    """A force delete goes through, and the state's history stays behind a delete marker."""
    enable_versioning()
    workspace_id = workspace["workspace_id"]
    add_variable(auth_client, workspace_id)
    write_state(workspace_id, state_with(2))
    seed_run(workspace_id, "run-01JBQ0000000000000000000R1", "applied")
    response = auth_client.delete(f"{BASE}/{workspace_id}", params={"force": "true"})
    assert response.status_code == 204, response.text
    assert auth_client.get(f"{BASE}/{workspace_id}").status_code == 404
    assert not state_exists(workspace_id)
    assert not run_exists("run-01JBQ0000000000000000000R1")
    listing = boto3.client("s3", region_name=REGION).list_object_versions(
        Bucket=STATE_BUCKET, Prefix=state_key(workspace_id)
    )
    assert [marker.get("IsLatest") for marker in listing.get("DeleteMarkers", [])] == [True]
    assert len(listing.get("Versions", [])) == 1


def test_safe_delete_goes_through_when_every_resource_is_empty(auth_client, workspace):
    """A state whose resources hold no instances, as after a destroy, deletes cleanly."""
    workspace_id = workspace["workspace_id"]
    write_state(workspace_id, state_with(0, 0))
    assert auth_client.delete(f"{BASE}/{workspace_id}").status_code == 204
    assert not state_exists(workspace_id)


def test_safe_delete_goes_through_with_no_state_object(auth_client, workspace):
    """A workspace that never wrote state deletes cleanly."""
    workspace_id = workspace["workspace_id"]
    assert auth_client.delete(f"{BASE}/{workspace_id}").status_code == 204
    assert auth_client.get(f"{BASE}/{workspace_id}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"resources": {}}', b'{"resources": ["x"]}', b'{"resources": [{"instances": {}}]}'],
)
def test_safe_delete_fails_closed_on_a_state_it_cannot_read(auth_client, workspace, body):
    """A state the check cannot parse counts as managing resources."""
    workspace_id = workspace["workspace_id"]
    write_state(workspace_id, body)
    response = auth_client.delete(f"{BASE}/{workspace_id}")
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "WORKSPACE_MANAGES_RESOURCES"
    assert state_exists(workspace_id)


@pytest.mark.parametrize("status", ["pending", "planning", "awaiting_confirmation", "applying", "some_new_status"])
@pytest.mark.parametrize("force", [False, True])
def test_delete_refuses_while_a_run_is_unfinished(auth_client, workspace, status, force):
    """An unfinished run blocks either mode with its own code, naming the run."""
    workspace_id = workspace["workspace_id"]
    add_variable(auth_client, workspace_id)
    seed_run(workspace_id, "run-01JBQ0000000000000000000R1", "applied", "2026-09-24T00:00:00Z")
    seed_run(workspace_id, "run-01JBQ0000000000000000000R2", status)
    response = auth_client.delete(f"{BASE}/{workspace_id}", params={"force": str(force).lower()})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "WORKSPACE_HAS_ACTIVE_RUN"
    assert "run-01JBQ0000000000000000000R2" in body["message"]
    assert_untouched(auth_client, workspace_id)
    assert run_exists("run-01JBQ0000000000000000000R1")
    assert run_exists("run-01JBQ0000000000000000000R2")


def test_delete_removes_only_this_workspaces_finished_runs(auth_client, workspace):
    """Every finished run on the workspace goes; another workspace's and the semaphore stay."""
    workspace_id = workspace["workspace_id"]
    for index, status in enumerate(["applied", "planned_and_finished", "errored", "cancelled", "discarded"]):
        seed_run(workspace_id, f"run-01JBQ000000000000000000R{index}", status, f"2026-09-2{index}T00:00:00Z")
    seed_run("ws-01JBQ0000000000000000000ZZ", "run-01JBQ0000000000000000000OT", "applied")
    repositories.runs().put({"run_id": SEMAPHORE_RUN_ID, "count": 0})
    assert auth_client.delete(f"{BASE}/{workspace_id}").status_code == 204
    for index in range(5):
        assert not run_exists(f"run-01JBQ000000000000000000R{index}")
    assert run_exists("run-01JBQ0000000000000000000OT")
    assert run_exists(SEMAPHORE_RUN_ID)


def test_delete_is_retryable_after_a_partial_failure(auth_client, workspace, monkeypatch):
    """A failure after the checks leaves the row, so a second delete finishes the job."""
    from app.domains.workspaces import state_versions

    workspace_id = workspace["workspace_id"]
    seed_run(workspace_id, "run-01JBQ0000000000000000000R1", "applied")
    original = state_versions.delete_current_state

    def failing(*args, **kwargs):
        """Fail the state delete once, as a throttled call would."""
        raise RuntimeError("simulated")

    monkeypatch.setattr(state_versions, "delete_current_state", failing)
    with pytest.raises(RuntimeError):
        auth_client.delete(f"{BASE}/{workspace_id}")
    assert auth_client.get(f"{BASE}/{workspace_id}").status_code == 200
    monkeypatch.setattr(state_versions, "delete_current_state", original)
    assert auth_client.delete(f"{BASE}/{workspace_id}").status_code == 204
    assert auth_client.get(f"{BASE}/{workspace_id}").status_code == 404
