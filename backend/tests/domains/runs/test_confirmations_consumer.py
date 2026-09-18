"""The confirmations consumer: the queue route the state machine's token arrives on.

Driven through the HTTP route rather than the handler alone, because the route is
what the Lambda Web Adapter posts to and the batch failure envelope is the only
thing the event source mapping reads. A record that fails must come back named in
`batchItemFailures`, or SQS deletes a task token nobody else holds.
"""

import json

import pytest
from fastapi.testclient import TestClient
from webbpulse.events import BATCH_FAILURES_KEY, FAILURE_ITEM_KEY, events_path

from app.common.composition.wiring import build_domain_app
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import confirmations

TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAAtask-token"


def message(run_id: str, task_token: str = TASK_TOKEN, *, kind: str = confirmations.CONFIRMATION_KIND) -> dict:
    """One SQS record carrying the body the state machine sends."""
    return {
        "messageId": f"msg-{run_id}",
        "body": json.dumps({"kind": kind, "run_id": run_id, "task_token": task_token}),
    }


def raw_message(body: str, message_id: str = "msg-raw") -> dict:
    """One SQS record carrying an arbitrary body, for the malformed cases."""
    return {"messageId": message_id, "body": body}


@pytest.fixture
def events_client(settings):
    """A client over the runs function, which is where the consumer route lives."""
    with TestClient(build_domain_app("runs", settings=settings)) as client:
        yield client


def post_batch(client: TestClient, *records: dict):
    """Post one batch to the consumer route and return the response."""
    return client.post(events_path(), json={"Records": list(records)})


def failed_ids(response) -> set[str]:
    """The message ids the response asked SQS to retry."""
    return {item[FAILURE_ITEM_KEY] for item in response.json()[BATCH_FAILURES_KEY]}


def test_a_confirmation_stores_the_token(events_client, planned_with_changes):
    """The happy path: the token lands on the run and nothing is retried."""
    run_id = planned_with_changes["run_id"]
    response = post_batch(events_client, message(run_id))

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []
    assert runs_service.get_run(run_id)["confirm_task_token"] == TASK_TOKEN


def test_a_stored_token_lets_the_run_be_confirmed(events_client, auth_client, planned_with_changes):
    """The token the consumer stored is the one `POST /confirm` sends success on."""
    run_id = planned_with_changes["run_id"]
    assert post_batch(events_client, message(run_id)).status_code == 200

    response = auth_client.post(f"/api/v1/runs/{run_id}/confirm")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applying"


def test_an_unknown_run_is_a_batch_item_failure(events_client):
    """A token for a run that does not exist is retried rather than dropped."""
    record = message("run-01JQZZZZZZZZZZZZZZZZZZZZZZ")
    response = post_batch(events_client, record)

    assert response.status_code == 200
    assert failed_ids(response) == {record["messageId"]}


def test_a_run_in_another_status_is_a_batch_item_failure(events_client, auth_client, planned_with_changes):
    """A run discarded before the message arrived does not take the token.

    Storing it would leave a finished run holding a live task token, which a later
    confirm would try to send success on.
    """
    run_id = planned_with_changes["run_id"]
    assert auth_client.post(f"/api/v1/runs/{run_id}/discard").status_code == 200

    record = message(run_id)
    response = post_batch(events_client, record)

    assert failed_ids(response) == {record["messageId"]}
    assert not runs_service.get_run(run_id).get("confirm_task_token")


def test_a_run_still_planning_is_a_batch_item_failure(events_client, created_run):
    """A token cannot land before the plan phase reported, so the record retries."""
    record = message(created_run["run_id"])
    response = post_batch(events_client, record)

    assert failed_ids(response) == {record["messageId"]}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("not json at all", id="not-json"),
        pytest.param(json.dumps(["a", "list"]), id="not-an-object"),
        pytest.param(json.dumps({"run_id": "run-x", "task_token": TASK_TOKEN}), id="no-kind"),
        pytest.param(
            json.dumps({"kind": "something_else", "run_id": "run-x", "task_token": TASK_TOKEN}), id="wrong-kind"
        ),
        pytest.param(json.dumps({"kind": confirmations.CONFIRMATION_KIND, "task_token": TASK_TOKEN}), id="no-run-id"),
        pytest.param(json.dumps({"kind": confirmations.CONFIRMATION_KIND, "run_id": "run-x"}), id="no-task-token"),
        pytest.param("", id="empty"),
    ],
)
def test_a_malformed_body_is_a_batch_item_failure(events_client, body):
    """A body this consumer cannot read parks rather than disappearing.

    Every one of these is permanent, so the retries are wasted, but a token dropped
    on the floor leaves an execution waiting out its whole confirmation timeout.
    """
    record = raw_message(body)
    response = post_batch(events_client, record)

    assert response.status_code == 200
    assert failed_ids(response) == {record["messageId"]}


def test_only_the_failing_record_is_retried(events_client, planned_with_changes):
    """A batch reports the records that raised and keeps the ones that did not."""
    run_id = planned_with_changes["run_id"]
    good = message(run_id)
    bad = raw_message("not json at all", message_id="msg-bad")

    response = post_batch(events_client, good, bad)

    assert failed_ids(response) == {bad["messageId"]}
    assert runs_service.get_run(run_id)["confirm_task_token"] == TASK_TOKEN


def test_the_route_is_not_under_the_api_prefix(settings):
    """The consumer path is never something the HTTP API could route."""
    assert not events_path().startswith("/api/v1")


def test_the_route_is_absent_from_the_schema(events_client):
    """The consumer is not part of any API, so it is not in the document."""
    schema = events_client.get("/openapi.json").json()
    assert events_path() not in schema["paths"]


def test_a_gateway_request_is_refused(events_client, planned_with_changes):
    """A request carrying an API Gateway request context gets a 404.

    The consumer answers the adapter's pass-through and nothing else, so a route
    reachable through the gateway would be a way to plant a task token.
    """
    from webbpulse.http import REQUEST_CONTEXT_HEADER

    response = events_client.post(
        events_path(),
        json={"Records": [message(planned_with_changes["run_id"])]},
        headers={REQUEST_CONTEXT_HEADER: json.dumps({"requestId": "abc", "http": {"method": "POST"}})},
    )

    assert response.status_code == 404
    assert not runs_service.get_run(planned_with_changes["run_id"]).get("confirm_task_token")


def test_parse_body_reads_the_state_machine_shape():
    """The body the `AwaitConfirmation` state sends parses to its two fields."""
    run_id, task_token = confirmations.parse_body(message("run-01JQ"))
    assert (run_id, task_token) == ("run-01JQ", TASK_TOKEN)
