"""The `tfe.v2` workspace locks, held in the S3 backend's own `terraform.tfstate.tflock`.

The cloud backend locks before every local state operation and unlocks after, and
`terraform force-unlock` clears a lock it left. go-tfe tells its errors apart by the
409's wording, so these pin that wording as well as the lockfile itself.
"""

from __future__ import annotations

import json

import boto3
from botocore.exceptions import ClientError

from app.common.core.auth import STATE_WRITE, WORKSPACES_READ
from app.common.db import repositories
from app.domains.workspaces import locks
from tests.conftest import REGION, STATE_BUCKET, mint_key

API = "/api/v2"
JSON_API = "application/vnd.api+json"


def _action(client, workspace_id: str, action: str, body: dict | None = None):
    """POST a lock action the way go-tfe does."""
    return client.post(
        f"{API}/workspaces/{workspace_id}/actions/{action}",
        content=json.dumps(body or {}),
        headers={"Content-Type": JSON_API},
    )


def _detail(response) -> str:
    """The first error's title and detail, as go-tfe joins them to match on."""
    error = response.json()["errors"][0]
    return f"{error.get('title', '')}\n\n{error.get('detail', '')}"


def _lockfile(workspace_id: str) -> dict | None:
    """The lockfile's body, or None when there is none."""
    try:
        response = boto3.client("s3", region_name=REGION).get_object(
            Bucket=STATE_BUCKET, Key=locks.lock_key(workspace_id)
        )
    except ClientError:
        return None
    return json.loads(response["Body"].read())


def _engine_lock(workspace_id: str) -> None:
    """Write a lockfile the way a run's engine does, with no CLI holder named."""
    body = {"ID": "engine-lock", "Operation": "OperationTypeApply", "Who": "runner"}
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=STATE_BUCKET, Key=locks.lock_key(workspace_id), Body=json.dumps(body).encode()
    )


def _seed_run(workspace_id: str, status: str, *, plan_only: bool = False) -> str:
    """Write one run row, as the runs domain would, and return its id."""
    run_id = "run-01JBQ0000000000000000000RA"
    repositories.runs().put(
        {
            "run_id": run_id,
            "workspace_id": workspace_id,
            "status": status,
            "plan_only": plan_only,
            "created_at": "2026-10-01T00:00:00Z",
        }
    )
    return run_id


def _other_client(app):
    """A client for a second person holding every scope."""
    from fastapi.testclient import TestClient

    token = mint_key(user_id="user-other")
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_lock_writes_the_runners_lockfile_and_reads_locked(auth_client, workspace):
    """A lock is the S3 backend's own lockfile, and the workspace then reads `locked`."""
    workspace_id = workspace["workspace_id"]
    response = _action(auth_client, workspace_id, "lock", {"reason": "Locked by Terraform"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["attributes"]["locked"] is True
    held = _lockfile(workspace_id)
    assert held is not None
    assert held["Info"] == "Locked by Terraform"
    assert held[locks.LOCK_OWNER_FIELD] == "user-test"
    assert locks.lock_key(workspace_id) == f"workspaces/{workspace_id}/terraform.tfstate.tflock"
    read = auth_client.get(f"{API}/workspaces/{workspace_id}")
    assert read.json()["data"]["attributes"]["locked"] is True


def test_a_second_lock_is_refused(auth_client, workspace):
    """The write is conditional, so a held lock answers go-tfe's `ErrWorkspaceLocked` 409."""
    workspace_id = workspace["workspace_id"]
    assert _action(auth_client, workspace_id, "lock").status_code == 200
    again = _action(auth_client, workspace_id, "lock")
    assert again.status_code == 409
    assert again.json()["errors"][0]["status"] == "409"


def test_an_engine_lock_refuses_a_cli_lock(auth_client, workspace):
    """A run's engine and the CLI exclude each other through the one object."""
    workspace_id = workspace["workspace_id"]
    _engine_lock(workspace_id)
    assert _action(auth_client, workspace_id, "lock").status_code == 409


def test_lock_is_refused_while_a_run_is_going(auth_client, workspace):
    """A run that is executing may be about to take the lock, so the CLI may not."""
    workspace_id = workspace["workspace_id"]
    run_id = _seed_run(workspace_id, "planning")
    response = _action(auth_client, workspace_id, "lock")
    assert response.status_code == 409
    assert run_id in _detail(response)
    assert _lockfile(workspace_id) is None


def test_a_plan_only_run_going_does_not_refuse_a_lock(auth_client, workspace):
    """A plan only run reads state under `-lock=false`, so it never stands in the CLI's way."""
    workspace_id = workspace["workspace_id"]
    _seed_run(workspace_id, "planning", plan_only=True)
    assert _action(auth_client, workspace_id, "lock").status_code == 200
    assert _lockfile(workspace_id) is not None


def test_unlock_by_the_holder_removes_the_lock(auth_client, workspace):
    """The subject that locked unlocks, and the workspace reads unlocked again."""
    workspace_id = workspace["workspace_id"]
    _action(auth_client, workspace_id, "lock")
    response = _action(auth_client, workspace_id, "unlock")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["attributes"]["locked"] is False
    assert _lockfile(workspace_id) is None
    read = auth_client.get(f"{API}/workspaces/{workspace_id}")
    assert read.json()["data"]["attributes"]["locked"] is False


def test_unlock_by_another_person_is_locked_by_user(app, auth_client, workspace):
    """go-tfe maps "is locked by User" to `ErrWorkspaceLockedByUser`."""
    workspace_id = workspace["workspace_id"]
    _action(auth_client, workspace_id, "lock")
    with _other_client(app) as other:
        response = _action(other, workspace_id, "unlock")
    assert response.status_code == 409
    assert "is locked by User" in _detail(response)
    assert _lockfile(workspace_id) is not None


def test_unlock_of_an_engine_lock_is_locked_by_run(auth_client, workspace):
    """go-tfe maps "is locked by Run" to `ErrWorkspaceLockedByRun`."""
    workspace_id = workspace["workspace_id"]
    _engine_lock(workspace_id)
    response = _action(auth_client, workspace_id, "unlock")
    assert response.status_code == 409
    assert "is locked by Run" in _detail(response)


def test_unlock_without_a_lock_is_not_locked(auth_client, workspace):
    """Wording without a holder is go-tfe's `ErrWorkspaceNotLocked`."""
    response = _action(auth_client, workspace["workspace_id"], "unlock")
    assert response.status_code == 409
    detail = _detail(response)
    assert "not locked" in detail
    assert "is locked by" not in detail


def test_force_unlock_clears_another_persons_and_an_engines_lock(app, auth_client, workspace):
    """`terraform force-unlock` is the way past a lock someone else left, no break-glass."""
    workspace_id = workspace["workspace_id"]
    with _other_client(app) as other:
        _action(other, workspace_id, "lock")
    response = _action(auth_client, workspace_id, "force-unlock")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["attributes"]["locked"] is False
    assert _lockfile(workspace_id) is None
    _engine_lock(workspace_id)
    assert _action(auth_client, workspace_id, "force-unlock").status_code == 200
    assert _lockfile(workspace_id) is None


def test_force_unlock_is_refused_while_a_run_is_going(auth_client, workspace):
    """A run still going may hold its lock right now, so it is left in place."""
    workspace_id = workspace["workspace_id"]
    _engine_lock(workspace_id)
    _seed_run(workspace_id, "applying")
    response = _action(auth_client, workspace_id, "force-unlock")
    assert response.status_code == 409
    assert _lockfile(workspace_id) is not None


def test_force_unlock_is_allowed_while_only_a_plan_only_run_is_going(auth_client, workspace):
    """A plan only run holds no lock, so a lock left behind may still be cleared."""
    workspace_id = workspace["workspace_id"]
    _engine_lock(workspace_id)
    _seed_run(workspace_id, "planning", plan_only=True)
    assert _action(auth_client, workspace_id, "force-unlock").status_code == 200
    assert _lockfile(workspace_id) is None


def test_force_unlock_without_a_lock_is_409(auth_client, workspace):
    """Nothing to remove is the 409 go-tfe maps to `ErrWorkspaceNotLocked`."""
    assert _action(auth_client, workspace["workspace_id"], "force-unlock").status_code == 409


def test_lock_actions_require_state_write(scoped_client, workspace):
    """Locking touches state, so a key without `state:write` gets the JSON:API 403."""
    client = scoped_client(WORKSPACES_READ)
    for action in ("lock", "unlock", "force-unlock"):
        response = _action(client, workspace["workspace_id"], action)
        assert response.status_code == 403, action
        assert response.json()["errors"][0]["status"] == "403"


def test_state_write_alone_may_lock(scoped_client, workspace):
    """`state:write` is the whole gate, so force-unlock needs no admin."""
    client = scoped_client(STATE_WRITE)
    assert _action(client, workspace["workspace_id"], "lock").status_code == 200
    assert _action(client, workspace["workspace_id"], "force-unlock").status_code == 200


def test_lock_on_an_absent_workspace_is_404(auth_client):
    """An unknown workspace is the JSON:API 404."""
    response = _action(auth_client, "ws-01JBQ0000000000000000000AA", "lock")
    assert response.status_code == 404
