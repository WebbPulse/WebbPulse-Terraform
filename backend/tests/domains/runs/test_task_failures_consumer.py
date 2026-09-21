"""The task failure consumer: the path that fails a run whose task never started.

Driven through the HTTP route rather than the handler alone, for the same reason
the confirmations suite is: the route is what the Lambda Web Adapter posts to and
the batch failure envelope is the only thing the event source mapping reads.

`SendTaskFailure` is observed through a recording client rather than moto, because
what matters is the exact error and cause reaching Step Functions against the token
the ECS event carried, and moto has no live token to fail.
"""

import json

import pytest
from fastapi.testclient import TestClient
from webbpulse.events import BATCH_FAILURES_KEY, FAILURE_ITEM_KEY, events_path

from app.common.composition.wiring import build_domain_app
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import task_failures

TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAAphase-task-token"
STOPPED_REASON = "CannotPullContainerError: ref does not exist"
CLUSTER_ARN = "arn:aws:ecs:us-west-2:870550636948:cluster/webbpulse-terraform-test-runner"


class RecordingStepFunctions:
    """A Step Functions client that records `SendTaskFailure` instead of calling it.

    Optionally raises a `ClientError` with a given code, which is how the consumed
    token cases are driven.
    """

    def __init__(self, error_code: str = ""):
        self.calls: list[dict] = []
        self.error_code = error_code

    def send_task_failure(self, **kwargs):
        """Record the call, raising the configured error code when there is one."""
        self.calls.append(kwargs)
        if self.error_code:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": self.error_code, "Message": "no"}}, "SendTaskFailure")
        return {}


@pytest.fixture
def recorded(monkeypatch):
    """Swap the service's Step Functions client for a recording one."""

    def install(error_code: str = "") -> RecordingStepFunctions:
        client = RecordingStepFunctions(error_code)
        monkeypatch.setattr(runs_service, "_stepfunctions", lambda settings: client)
        return client

    return install


@pytest.fixture
def events_client(settings):
    """A client over the runs function, which is where the consumer route lives."""
    with TestClient(build_domain_app("runs", settings=settings)) as client:
        yield client


def detail(
    run_id: str,
    *,
    task_token: str = TASK_TOKEN,
    stop_code: str = task_failures.FAILED_TO_START_STOP_CODE,
    last_status: str = "STOPPED",
    stopped_reason: str = STOPPED_REASON,
    environment: list | None = None,
) -> dict:
    """The `detail` of one ECS Task State Change event for a phase task."""
    if environment is None:
        environment = [
            {"name": "RUN_ID", "value": run_id},
            {"name": "WORKSPACE_ID", "value": "ws-example"},
            {"name": "PHASE", "value": "plan"},
            {"name": "TASK_TOKEN", "value": task_token},
        ]
    return {
        "clusterArn": CLUSTER_ARN,
        "taskArn": f"{CLUSTER_ARN.replace(':cluster/', ':task/')}/abc123",
        "lastStatus": last_status,
        "desiredStatus": "STOPPED",
        "stopCode": stop_code,
        "stoppedReason": stopped_reason,
        "overrides": {"containerOverrides": [{"name": "plan", "environment": environment}]},
    }


def message(run_id: str, *, kind: str = task_failures.TASK_FAILURE_KIND, **kwargs) -> dict:
    """One SQS record carrying the body the EventBridge rule's transformer builds."""
    return {
        "messageId": f"msg-{run_id}",
        "body": json.dumps({"kind": kind, "detail": detail(run_id, **kwargs)}),
    }


def raw_message(body: str, message_id: str = "msg-raw") -> dict:
    """One SQS record carrying an arbitrary body, for the malformed cases."""
    return {"messageId": message_id, "body": body}


def post_batch(client: TestClient, *records: dict):
    """Post one batch to the consumer route and return the response."""
    return client.post(events_path(), json={"Records": list(records)})


def failed_ids(response) -> set[str]:
    """The message ids the response asked SQS to retry."""
    return {item[FAILURE_ITEM_KEY] for item in response.json()[BATCH_FAILURES_KEY]}


def test_a_failed_start_fails_the_phase_task_token(events_client, recorded, created_run):
    """The happy path: the token the task carried is failed with the ECS reason."""
    client = recorded()
    run_id = created_run["run_id"]

    response = post_batch(events_client, message(run_id))

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []
    assert client.calls == [
        {
            "taskToken": TASK_TOKEN,
            "error": task_failures.TASK_FAILURE_ERROR,
            "cause": STOPPED_REASON,
        }
    ]


def test_a_task_with_no_stopped_reason_still_carries_a_cause(events_client, recorded, created_run):
    """`SendTaskFailure` always gets a cause, because an empty one hides the failure."""
    client = recorded()

    response = post_batch(events_client, message(created_run["run_id"], stopped_reason=""))

    assert response.status_code == 200
    assert client.calls[0]["cause"] == task_failures.DEFAULT_STOPPED_REASON


def test_the_run_row_is_left_to_the_state_machine(events_client, recorded, created_run):
    """The execution's own failure path owns the status and the semaphore.

    Writing `errored` here would race `MarkErrored`, and releasing the slot would
    take one the state machine is about to release again. The run is still
    `planning` afterwards because only `MarkErrored` moves it.
    """
    recorded()
    run_id = created_run["run_id"]

    post_batch(events_client, message(run_id))

    assert runs_service.get_run(run_id)["status"] == "planning"
    assert not runs_service.get_run(run_id).get("finished_at")


@pytest.mark.parametrize(
    "code",
    ["TaskTimedOut", "TaskDoesNotExist"],
)
def test_an_already_consumed_token_is_not_a_failure(events_client, recorded, created_run, code):
    """The runner reported first, or the heartbeat expired, so there is nothing to do.

    EventBridge delivers at least once, so this is the ordinary case for a retry and
    it must not park the message.
    """
    recorded(code)

    response = post_batch(events_client, message(created_run["run_id"]))

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []


def test_another_step_functions_error_is_a_batch_item_failure(events_client, recorded, created_run):
    """A real Step Functions fault retries rather than dropping a live token."""
    recorded("ThrottlingException")
    record = message(created_run["run_id"])

    response = post_batch(events_client, record)

    assert failed_ids(response) == {record["messageId"]}


def test_a_terminal_run_sends_nothing(events_client, recorded, created_run):
    """A run that already finished keeps its outcome; no token is failed.

    The ordinary race: the runner reported a result and the task then stopped, so
    the event arrives against a run the state machine has already resolved.
    """
    run_id = created_run["run_id"]
    runs_service.finish_run(run_id, "errored", error="The plan phase exited 1.")
    client = recorded()

    response = post_batch(events_client, message(run_id))

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []
    assert client.calls == []


def test_an_unknown_run_is_a_batch_item_failure(events_client, recorded):
    """A stop that raced its run row is retried rather than dropped."""
    recorded()
    record = message("run-01JQZZZZZZZZZZZZZZZZZZZZZZ")

    response = post_batch(events_client, record)

    assert failed_ids(response) == {record["messageId"]}


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"stop_code": "EssentialContainerExited"}, id="container-ran"),
        pytest.param({"stop_code": "UserInitiated"}, id="stopped-by-hand"),
        pytest.param({"stop_code": ""}, id="no-stop-code"),
        pytest.param({"last_status": "RUNNING"}, id="still-running"),
        pytest.param({"environment": []}, id="no-overrides"),
        pytest.param({"environment": [{"name": "RUN_ID", "value": "run-x"}]}, id="no-task-token"),
        pytest.param({"task_token": ""}, id="empty-task-token"),
    ],
)
def test_a_stop_this_consumer_does_not_own_is_acknowledged(events_client, recorded, created_run, kwargs):
    """Any other task stop is dropped, not retried.

    The rule is narrow but not exact, and every run ends with a task stopping. Parking
    each of those on the dead letter queue would bury the deliveries that matter.
    """
    client = recorded()

    response = post_batch(events_client, message(created_run["run_id"], **kwargs))

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []
    assert client.calls == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param(json.dumps(["a", "list"]), id="not-an-object"),
        pytest.param(json.dumps({"detail": {"lastStatus": "STOPPED"}}), id="no-kind"),
        pytest.param(json.dumps({"kind": "something_else", "detail": {}}), id="wrong-kind"),
        pytest.param(json.dumps({"kind": task_failures.TASK_FAILURE_KIND}), id="no-detail"),
        pytest.param(json.dumps({"kind": task_failures.TASK_FAILURE_KIND, "detail": "text"}), id="detail-not-object"),
        pytest.param("", id="empty"),
    ],
)
def test_a_malformed_body_is_a_batch_item_failure(events_client, body):
    """A body no consumer can read parks rather than disappearing."""
    record = raw_message(body)

    response = post_batch(events_client, record)

    assert response.status_code == 200
    assert failed_ids(response) == {record["messageId"]}


def test_parse_body_reads_the_event_shape():
    """The body the rule builds parses to the run, token and reason."""
    parsed = task_failures.parse_body(message("run-01JQ"))

    assert parsed == ("run-01JQ", TASK_TOKEN, STOPPED_REASON)


def test_a_cause_longer_than_step_functions_accepts_is_truncated(events_client, recorded, created_run):
    """An unusually long stopped reason must not fail the call that fails the run."""
    client = recorded()
    long_reason = "x" * (runs_service.CAUSE_MAX_LENGTH + 500)

    post_batch(events_client, message(created_run["run_id"], stopped_reason=long_reason))

    assert len(client.calls[0]["cause"]) == runs_service.CAUSE_MAX_LENGTH


def test_both_consumers_share_the_one_events_route(events_client, recorded, planned_with_changes):
    """A confirmation and a task stop in one batch each reach their own consumer.

    The adapter posts every queue invocation to a single pass-through path, so the
    dispatch on `kind` is the only thing keeping the two apart.
    """
    from app.domains.runs.consumers import confirmations

    client = recorded()
    run_id = planned_with_changes["run_id"]
    confirmation = {
        "messageId": "msg-confirmation",
        "body": json.dumps({"kind": confirmations.CONFIRMATION_KIND, "run_id": run_id, "task_token": "confirm-token"}),
    }
    stop = {
        "messageId": "msg-stop",
        "body": json.dumps({"kind": task_failures.TASK_FAILURE_KIND, "detail": detail(run_id)}),
    }

    response = post_batch(events_client, confirmation, stop)

    assert response.json()[BATCH_FAILURES_KEY] == []
    assert runs_service.get_run(run_id)["confirm_task_token"] == "confirm-token"
    assert client.calls[0]["taskToken"] == TASK_TOKEN
