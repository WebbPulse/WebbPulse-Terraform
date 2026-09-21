"""The logs route, its stream naming and its paging token."""

import time

import boto3

from app.domains.runs import service as runs_service
from app.domains.runs.schemas.run import Phase
from tests.conftest import REGION, RUNNER_LOG_GROUP

BASE = "/api/v1/runs"


def write_events(run_id: str, phase: Phase, messages: list[str]) -> int:
    """Put `messages` on the stream the runner would write for this phase.

    The timestamps are relative to now because CloudWatch Logs, and moto with it,
    silently discards events older than the group's retention window.
    """
    client = boto3.client("logs", region_name=REGION)
    stream = runs_service.log_stream_name(run_id, phase)
    client.create_log_stream(logGroupName=RUNNER_LOG_GROUP, logStreamName=stream)
    first = int(time.time() * 1000)
    client.put_log_events(
        logGroupName=RUNNER_LOG_GROUP,
        logStreamName=stream,
        logEvents=[{"timestamp": first + index * 1000, "message": message} for index, message in enumerate(messages)],
    )
    return first


def test_the_stream_name_follows_the_contract(created_run):
    """The stream is `<run_id>/<phase>`, which is what the runner writes to."""
    run_id = created_run["run_id"]
    assert runs_service.log_stream_name(run_id, "plan") == f"{run_id}/plan"
    assert runs_service.log_stream_name(run_id, "apply") == f"{run_id}/apply"


def test_logs_return_the_events(auth_client, runner_log_group, created_run):
    """A phase's events come back in order with their timestamps."""
    run_id = created_run["run_id"]
    first = write_events(run_id, "plan", ["Initializing...", "Plan: 1 to add"])

    response = auth_client.get(f"{BASE}/{run_id}/logs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == run_id
    assert body["phase"] == "plan"
    assert [event["message"] for event in body["events"]] == ["Initializing...", "Plan: 1 to add"]
    assert body["events"][0]["timestamp"] == first


def test_logs_default_to_the_plan_phase(auth_client, runner_log_group, created_run):
    """No phase means the plan, which is the phase every run has."""
    run_id = created_run["run_id"]
    write_events(run_id, "plan", ["planning"])
    assert auth_client.get(f"{BASE}/{run_id}/logs").json()["phase"] == "plan"


def test_logs_read_the_apply_phase_when_asked(auth_client, runner_log_group, created_run):
    """The apply phase reads from its own stream, not the plan's."""
    run_id = created_run["run_id"]
    write_events(run_id, "plan", ["planning"])
    write_events(run_id, "apply", ["applying"])

    body = auth_client.get(f"{BASE}/{run_id}/logs", params={"phase": "apply"}).json()
    assert body["phase"] == "apply"
    assert [event["message"] for event in body["events"]] == ["applying"]


def test_logs_carry_a_continuation_token(auth_client, runner_log_group, created_run):
    """A page hands back the token that continues it."""
    run_id = created_run["run_id"]
    write_events(run_id, "plan", ["one", "two"])
    assert auth_client.get(f"{BASE}/{run_id}/logs").json()["next_after"]


def test_the_continuation_token_is_accepted(auth_client, runner_log_group, created_run):
    """Passing the token back reads from where the last page stopped.

    A poller that could not resume would re-read the whole stream on every tick.
    """
    run_id = created_run["run_id"]
    write_events(run_id, "plan", ["one", "two"])
    first = auth_client.get(f"{BASE}/{run_id}/logs").json()

    second = auth_client.get(f"{BASE}/{run_id}/logs", params={"after": first["next_after"]})
    assert second.status_code == 200, second.text
    assert second.json()["events"] == []


def test_an_absent_stream_is_an_empty_page(auth_client, runner_log_group, created_run):
    """A run whose task has not started yet reads empty, not 404.

    A 404 would look like a missing run to a poller and stop it polling before
    the runner ever wrote a line.
    """
    response = auth_client.get(f"{BASE}/{created_run['run_id']}/logs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["events"] == []
    assert body["next_after"] is None


def test_an_absent_log_group_is_an_empty_page(auth_client, created_run):
    """With no group provisioned the route still answers, without erroring."""
    response = auth_client.get(f"{BASE}/{created_run['run_id']}/logs")
    assert response.status_code == 200, response.text
    assert response.json()["events"] == []


def test_logs_are_404_for_an_absent_run(auth_client, runner_log_group):
    """Logs for a run that is not there is a 404."""
    assert auth_client.get(f"{BASE}/run-01JBQ0000000000000000000AA/logs").status_code == 404


def test_an_unknown_phase_is_422(auth_client, created_run):
    """Only the two contract phases are readable."""
    response = auth_client.get(f"{BASE}/{created_run['run_id']}/logs", params={"phase": "destroy"})
    assert response.status_code == 422


def test_logs_need_a_read_scope(client, created_run):
    """An unauthenticated caller cannot read a run's logs."""
    assert client.get(f"{BASE}/{created_run['run_id']}/logs").status_code == 401
