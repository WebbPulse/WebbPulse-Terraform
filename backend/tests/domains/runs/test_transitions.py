"""Every status a run can reach, and the routes that move it there.

The transitions are the contract's state machine, so each one is asserted from
the status it is legal in and refused from one it is not.
"""

import boto3

from app.common.db.tables import RUNS, local_table_name
from app.domains.runs import service as runs_service
from tests.conftest import ENVIRONMENT, REGION

BASE = "/api/v1/runs"

PLAN_WITH_CHANGES = {
    "phase": "plan",
    "exit_code": 0,
    "changes": {"add": 2, "change": 1, "destroy": 0},
    "error": "",
}
PLAN_NO_CHANGES = {
    "phase": "plan",
    "exit_code": 0,
    "changes": {"add": 0, "change": 0, "destroy": 0},
    "error": "",
}


def stored_run(run_id: str) -> dict:
    """The raw run row, so stored-only fields are visible."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    return table.get_item(Key={"run_id": run_id}).get("Item", {})


def test_a_plan_with_changes_awaits_confirmation(created_run):
    """A plan that found changes stops for a human."""
    updated = runs_service.record_phase_result(created_run["run_id"], PLAN_WITH_CHANGES)
    assert updated["status"] == "awaiting_confirmation"
    assert updated["changes"] == {"add": 2, "change": 1, "destroy": 0}


def test_the_recorded_changes_are_not_the_status(created_run):
    """`changes` holds the plan counts, not a status string.

    A conditional update whose value placeholders collide with the condition's
    writes the guard's status into whichever attribute sorted first, which is
    `changes`. The row is asserted directly because the response model would
    reject the corrupt shape rather than reveal it.
    """
    runs_service.record_phase_result(created_run["run_id"], PLAN_WITH_CHANGES)
    assert stored_run(created_run["run_id"])["changes"] == {"add": 2, "change": 1, "destroy": 0}


def test_a_plan_with_no_changes_finishes(created_run):
    """Nothing to apply means the run ends at the plan."""
    updated = runs_service.record_phase_result(created_run["run_id"], PLAN_NO_CHANGES)
    assert updated["status"] == "planned_and_finished"
    assert updated["finished_at"]


def test_a_plan_only_run_finishes_even_with_changes(plan_only_run):
    """`plan_only` never reaches an apply, changes or not."""
    updated = runs_service.record_phase_result(plan_only_run["run_id"], PLAN_WITH_CHANGES)
    assert updated["status"] == "planned_and_finished"


def test_a_failed_plan_errors_the_run(created_run):
    """A non zero plan exit errors the run and keeps the message."""
    updated = runs_service.record_phase_result(
        created_run["run_id"],
        {"phase": "plan", "exit_code": 1, "changes": {}, "error": "invalid HCL"},
    )
    assert updated["status"] == "errored"
    assert updated["error"] == "invalid HCL"
    assert updated["finished_at"]


def test_a_failed_plan_without_a_message_still_errors(created_run):
    """An exit code with no message still produces an error string."""
    updated = runs_service.record_phase_result(
        created_run["run_id"],
        {"phase": "plan", "exit_code": 3, "changes": {}, "error": ""},
    )
    assert updated["status"] == "errored"
    assert "3" in updated["error"]


def test_a_plan_exiting_two_with_changes_awaits_confirmation(created_run):
    """Exit 2 is terraform's `-detailed-exitcode` for changes, not a failure.

    The runner reports 0 for it, so this covers a runner that posts the raw
    terraform code and would otherwise error every plan that found changes.
    """
    updated = runs_service.record_phase_result(
        created_run["run_id"],
        {"phase": "plan", "exit_code": 2, "changes": {"add": 2, "change": 1, "destroy": 0}, "error": ""},
    )
    assert updated["status"] == "awaiting_confirmation"
    assert updated["changes"] == {"add": 2, "change": 1, "destroy": 0}


def test_a_plan_only_run_exiting_two_finishes(plan_only_run):
    """A `plan_only` run reporting the detailed changes code still finishes."""
    updated = runs_service.record_phase_result(
        plan_only_run["run_id"],
        {"phase": "plan", "exit_code": 2, "changes": {"add": 1, "change": 0, "destroy": 0}, "error": ""},
    )
    assert updated["status"] == "planned_and_finished"


def test_an_apply_exiting_two_still_errors(auth_client, awaiting_confirmation):
    """Only the plan phase reads 2 as changes; an apply exiting 2 failed."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    updated = runs_service.record_phase_result(
        run_id,
        {"phase": "apply", "exit_code": 2, "changes": {}, "error": ""},
    )
    assert updated["status"] == "errored"


def test_confirm_moves_the_run_to_applying(auth_client, awaiting_confirmation):
    """A confirm advances the run and consumes its task token."""
    run_id = awaiting_confirmation["run_id"]
    response = auth_client.post(f"{BASE}/{run_id}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applying"
    assert not stored_run(run_id).get("confirm_task_token")


def test_a_second_confirm_is_409(auth_client, awaiting_confirmation):
    """Only the first confirm wins, so a double click cannot apply twice."""
    run_id = awaiting_confirmation["run_id"]
    assert auth_client.post(f"{BASE}/{run_id}/confirm").status_code == 200
    assert auth_client.post(f"{BASE}/{run_id}/confirm").status_code == 409


def test_confirm_is_409_while_still_planning(auth_client, created_run):
    """A run that has not planned yet has nothing to confirm."""
    response = auth_client.post(f"{BASE}/{created_run['run_id']}/confirm")
    assert response.status_code == 409


def test_confirm_is_404_for_an_absent_run(auth_client):
    """Confirming nothing is a 404."""
    assert auth_client.post(f"{BASE}/run-01JBQ0000000000000000000AA/confirm").status_code == 404


def test_a_successful_apply_applies_the_run(auth_client, awaiting_confirmation):
    """The apply phase's success is the run's final state."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    updated = runs_service.record_phase_result(
        run_id,
        {"phase": "apply", "exit_code": 0, "changes": {"add": 2, "change": 1, "destroy": 0}, "error": ""},
    )
    assert updated["status"] == "applied"
    assert updated["finished_at"]


def test_a_failed_apply_errors_the_run(auth_client, awaiting_confirmation):
    """A failed apply errors rather than applying."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/confirm")
    updated = runs_service.record_phase_result(
        run_id,
        {"phase": "apply", "exit_code": 1, "changes": {}, "error": "AccessDenied"},
    )
    assert updated["status"] == "errored"
    assert updated["error"] == "AccessDenied"


def test_a_phase_result_for_the_wrong_phase_is_refused(created_run):
    """An apply result on a planning run is a phase mismatch.

    Accepting it would let a runner skip the confirmation gate by reporting an
    apply that never happened.
    """
    import pytest

    with pytest.raises(runs_service.PhaseMismatch):
        runs_service.record_phase_result(
            created_run["run_id"],
            {"phase": "apply", "exit_code": 0, "changes": {}, "error": ""},
        )


def test_a_phase_result_on_a_finished_run_is_refused(created_run):
    """A terminal run accepts no further phase results."""
    import pytest

    runs_service.record_phase_result(created_run["run_id"], PLAN_NO_CHANGES)
    with pytest.raises(runs_service.PhaseMismatch):
        runs_service.record_phase_result(created_run["run_id"], PLAN_WITH_CHANGES)


def test_cancel_stops_a_planning_run(auth_client, created_run):
    """A cancel ends a run that is mid phase."""
    response = auth_client.post(f"{BASE}/{created_run['run_id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["finished_at"]


def test_cancel_stops_an_awaiting_run(auth_client, awaiting_confirmation):
    """A run waiting on a human can be cancelled rather than confirmed."""
    response = auth_client.post(f"{BASE}/{awaiting_confirmation['run_id']}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"


def test_cancel_is_409_for_a_finished_run(auth_client, created_run):
    """A finished run cannot be cancelled."""
    runs_service.record_phase_result(created_run["run_id"], PLAN_NO_CHANGES)
    assert auth_client.post(f"{BASE}/{created_run['run_id']}/cancel").status_code == 409


def test_cancel_is_404_for_an_absent_run(auth_client):
    """Cancelling nothing is a 404."""
    assert auth_client.post(f"{BASE}/run-01JBQ0000000000000000000AA/cancel").status_code == 404


def test_discard_drops_a_plan_awaiting_confirmation(auth_client, awaiting_confirmation):
    """A discard ends the run without applying its plan."""
    response = auth_client.post(f"{BASE}/{awaiting_confirmation['run_id']}/discard")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "discarded"
    assert response.json()["finished_at"]


def test_discard_clears_the_task_token(auth_client, awaiting_confirmation):
    """The confirm token is gone once the run is discarded."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/discard")
    assert not stored_run(run_id).get("confirm_task_token")


def test_discard_is_409_while_still_planning(auth_client, created_run):
    """Before a plan exists there is nothing to discard, so cancel is the verb."""
    assert auth_client.post(f"{BASE}/{created_run['run_id']}/discard").status_code == 409


def test_discard_is_409_for_a_finished_run(auth_client, awaiting_confirmation):
    """An applied run cannot be discarded after the fact."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{BASE}/{run_id}/discard")
    assert auth_client.post(f"{BASE}/{run_id}/discard").status_code == 409


def test_discard_is_404_for_an_absent_run(auth_client):
    """Discarding nothing is a 404."""
    assert auth_client.post(f"{BASE}/run-01JBQ0000000000000000000AA/discard").status_code == 404


def test_discard_leaves_the_run_discarded_not_errored(auth_client, awaiting_confirmation):
    """A discard is a discard on the row, not an error.

    The regression this guards: the confirmation wait used to catch the discard's
    `RunDiscarded` task failure under `States.ALL` and mark the run errored over the
    top of the status written here. The stored row is asserted rather than the
    response, because the response is the write and the row is what survives it.
    """
    run_id = awaiting_confirmation["run_id"]
    assert auth_client.post(f"{BASE}/{run_id}/discard").status_code == 200

    stored = stored_run(run_id)
    assert stored["status"] == "discarded"
    assert stored["finished_at"]


def test_a_confirmation_timeout_errors_the_run(awaiting_confirmation):
    """A confirmation nobody answered is a failure, so the run errors.

    The other side of the discard fix. `States.Timeout` on the confirmation wait
    still reaches `MarkErrored`, which lands here as an ordinary error transition
    on a run that was never finished by anything else.
    """
    run_id = awaiting_confirmation["run_id"]
    updated = runs_service.finish_run(
        run_id,
        "errored",
        error="The confirmation timed out before anyone answered it.",
    )
    assert updated["status"] == "errored"
    assert updated["error"] == "The confirmation timed out before anyone answered it."
    assert not stored_run(run_id).get("confirm_task_token")


def test_a_terminal_status_is_not_overwritten(auth_client, awaiting_confirmation):
    """A second ending does not relabel the first one.

    The backend half of the race the state machine catch fixes: even if a state
    machine path reaches `finish_run` behind a discard, the discarded status stands
    and the later writer is told what the run actually is.
    """
    run_id = awaiting_confirmation["run_id"]
    assert auth_client.post(f"{BASE}/{run_id}/discard").status_code == 200

    updated = runs_service.finish_run(run_id, "errored", error="A late error.")

    assert updated["status"] == "discarded"
    assert not updated.get("error")
    assert stored_run(run_id)["status"] == "discarded"


def test_a_terminal_transition_revokes_the_run_token(runner_client, created_run):
    """A finished run's token stops working, so it cannot outlive the run.

    Asserted through the runner's own route: the token authenticated before the
    run finished and must not afterwards.
    """
    run_id = created_run["run_id"]
    assert runner_client.get(f"{BASE}/{run_id}/bundle").status_code == 200

    runs_service.finish_run(run_id, "applied")

    assert runner_client.get(f"{BASE}/{run_id}/bundle").status_code == 401


def test_cancel_revokes_the_run_token(runner_client, created_run):
    """A cancelled run's token is dead too, not only a successful one."""
    run_id = created_run["run_id"]
    runs_service.cancel_run(run_id)
    assert runner_client.get(f"{BASE}/{run_id}/bundle").status_code == 401
