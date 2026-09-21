"""The concurrency semaphore's release on cancel and its prune on create.

The state machine takes a slot in `AcquireSemaphore` and gives it back in one of
the two release states. A cancel stops the execution outright, so neither release
state ever runs and the slot would be held by a finished run forever. These cover
both halves of the fix: the explicit release on the cancel path, and the prune
that runs before every start so a slot leaked by any cause heals itself.

The semaphore row is seeded and read with boto3 around the service, the way
`stored_config_version_status` reads config version rows, because no service call
writes it in the suite: the real writer is the state machine.
"""

import boto3

from app.common.db.tables import RUNS, SEMAPHORE_RUN_ID, local_table_name
from app.domains.runs import service as runs_service
from tests.conftest import ENVIRONMENT, REGION

BASE = "/api/v1/runs"

PLAN_NO_CHANGES = {
    "phase": "plan",
    "exit_code": 0,
    "changes": {"add": 0, "change": 0, "destroy": 0},
    "error": "",
}


def runs_table():
    """The raw runs table, for reads and writes around the service."""
    return boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))


def seed_holders(*run_ids: str) -> None:
    """Put the semaphore row holding `run_ids`, the way `AcquireSemaphore` would."""
    runs_table().put_item(Item={"run_id": SEMAPHORE_RUN_ID, "holders": set(run_ids)})


def stored_holders() -> set[str]:
    """The `holders` set on the raw semaphore row, empty when it is gone."""
    item = runs_table().get_item(Key={"run_id": SEMAPHORE_RUN_ID}).get("Item", {})
    return {str(holder) for holder in item.get("holders") or set()}


def test_cancel_removes_the_holder_from_the_set(auth_client, created_run):
    """A cancelled run gives its slot back, since no release state will run for it."""
    run_id = created_run["run_id"]
    seed_holders(run_id, "run-01JBQ0000000000000000000AA")

    response = auth_client.post(f"{BASE}/{run_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"

    assert stored_holders() == {"run-01JBQ0000000000000000000AA"}


def test_cancel_leaves_the_other_holders_alone(auth_client, created_run):
    """The release is a set DELETE of one id, not a rewrite of the whole set."""
    run_id = created_run["run_id"]
    others = {"run-01JBQ0000000000000000000AA", "run-01JBQ0000000000000000000BB"}
    seed_holders(run_id, *others)

    auth_client.post(f"{BASE}/{run_id}/cancel")

    assert stored_holders() == others


def test_releasing_a_run_that_holds_nothing_is_a_no_op(created_run):
    """The release is unconditional and idempotent, so a double cancel is safe."""
    seed_holders("run-01JBQ0000000000000000000AA")

    runs_service.release_semaphore(created_run["run_id"])
    runs_service.release_semaphore(created_run["run_id"])

    assert stored_holders() == {"run-01JBQ0000000000000000000AA"}


def test_prune_removes_a_cancelled_holder(auth_client, created_run):
    """A holder whose run reached a terminal status has no claim on a slot."""
    run_id = created_run["run_id"]
    auth_client.post(f"{BASE}/{run_id}/cancel")
    seed_holders(run_id)

    assert runs_service.prune_semaphore() == [run_id]
    assert stored_holders() == set()


def test_prune_removes_a_missing_holder(created_run):
    """A holder whose run row is gone entirely is pruned too."""
    seed_holders("run-01JBQ0000000000000000000AA")

    assert runs_service.prune_semaphore() == ["run-01JBQ0000000000000000000AA"]
    assert stored_holders() == set()


def test_prune_keeps_a_planning_holder(created_run):
    """A run still executing holds its slot legitimately and must survive a prune."""
    run_id = created_run["run_id"]
    assert runs_service.get_run(run_id)["status"] == "planning"
    seed_holders(run_id)

    assert runs_service.prune_semaphore() == []
    assert stored_holders() == {run_id}


def test_prune_is_a_no_op_without_a_semaphore_row(app):
    """A fresh environment has no row until the first acquire, which is not an error."""
    assert runs_service.prune_semaphore() == []
    assert stored_holders() == set()


def test_creating_a_run_prunes_a_leaked_terminal_holder(auth_client, workspace, uploaded_config_version, state_machine):
    """A slot leaked by a cancelled run is reclaimed by the next create.

    This is the whole point of the prune: staging sat with two cancelled runs in
    `holders` and every new run retried `AcquireSemaphore` for an hour. Starting
    any run now clears them.
    """
    workspace_id = workspace["workspace_id"]
    body = {
        "workspace_id": workspace_id,
        "config_version_id": uploaded_config_version["config_version_id"],
        "plan_only": False,
    }

    first = auth_client.post(BASE, json=body)
    assert first.status_code == 201, first.text
    leaked = first.json()["run_id"]
    auth_client.post(f"{BASE}/{leaked}/cancel")

    seed_holders(leaked)

    second = auth_client.post(BASE, json=body)
    assert second.status_code == 201, second.text
    assert second.json()["status"] == "planning"
    assert stored_holders() == set()


def test_get_run_refuses_the_semaphore_key(app):
    """The semaphore row is not a run, so asking for it by id is a 404."""
    import pytest

    seed_holders("run-01JBQ0000000000000000000AA")

    with pytest.raises(runs_service.RunNotFound):
        runs_service.get_run(SEMAPHORE_RUN_ID)


def test_the_semaphore_row_never_appears_in_a_workspace_listing(auth_client, created_run, workspace):
    """The row carries no `workspace_id`, so the `by_workspace` GSI cannot return it."""
    seed_holders(created_run["run_id"])

    listed = runs_service.list_runs(workspace["workspace_id"])

    assert [item["run_id"] for item in listed] == [created_run["run_id"]]
    assert SEMAPHORE_RUN_ID not in {item["run_id"] for item in listed}
