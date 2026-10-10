"""The execution endings consumer: finishing a run its execution left live."""

import json

import pytest
from fastapi.testclient import TestClient
from webbpulse.events import BATCH_FAILURES_KEY, FAILURE_ITEM_KEY, events_path

from app.common.composition.wiring import build_domain_app
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import execution_endings


@pytest.fixture
def events_client(settings):
    """A client over the runs function, which is where the consumer route lives."""
    with TestClient(build_domain_app("runs", settings=settings)) as client:
        yield client


@pytest.fixture
def released(monkeypatch):
    """Record every run id the consumer drops from the semaphore."""
    calls: list[str] = []
    monkeypatch.setattr(runs_service, "release_semaphore", lambda run_id, settings=None: calls.append(run_id))
    return calls


def message(run_id: str, status: str = "FAILED", error: str | None = "States.Runtime") -> dict:
    """One SQS record carrying the body the EventBridge rule's transformer builds."""
    detail = {
        "executionArn": f"arn:aws:states:us-west-2:111111111111:execution:test-run:{run_id}",
        "name": run_id,
        "status": status,
        "error": error,
        "cause": None,
    }
    return {
        "messageId": f"msg-{run_id}-{status}",
        "body": json.dumps({"kind": execution_endings.KIND, "detail": detail}),
    }


def post_batch(client: TestClient, *records: dict):
    """Post one batch to the consumer route and return the response."""
    return client.post(events_path(), json={"Records": list(records)})


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT"])
def test_a_live_run_whose_execution_failed_is_errored(events_client, created_run, released, status):
    run_id = created_run["run_id"]

    response = post_batch(events_client, message(run_id, status))

    assert response.json()[BATCH_FAILURES_KEY] == []
    run = runs_service.get_run(run_id)
    assert run["status"] == "errored"
    assert run["finished_at"]
    assert released == [run_id]


def test_an_execution_stopped_by_cancel_cancels_the_run(events_client, created_run, released):
    run_id = created_run["run_id"]

    post_batch(events_client, message(run_id, "ABORTED", execution_endings.CANCELLED_ERROR))

    assert runs_service.get_run(run_id)["status"] == "cancelled"


def test_a_finished_run_keeps_its_status(events_client, created_run, released):
    run_id = created_run["run_id"]
    runs_service.finish_run(run_id, "errored", error="The plan phase exited 1.")

    response = post_batch(events_client, message(run_id))

    assert response.json()[BATCH_FAILURES_KEY] == []
    assert runs_service.get_run(run_id)["error"] == "The plan phase exited 1."
    assert released == [run_id]


def test_a_succeeded_execution_is_ignored(events_client, created_run, released):
    run_id = created_run["run_id"]

    post_batch(events_client, message(run_id, "SUCCEEDED"))

    assert runs_service.get_run(run_id)["status"] == "planning"
    assert released == []


def test_an_unknown_run_is_dropped(events_client, released):
    response = post_batch(events_client, message("run-01JBQ0000000000000000000ZZ"))

    assert response.json()[BATCH_FAILURES_KEY] == []
    assert released == []


def test_a_body_without_an_execution_name_is_retried(events_client):
    record = {"messageId": "msg-bad", "body": json.dumps({"kind": execution_endings.KIND, "detail": {}})}

    response = post_batch(events_client, record)

    assert {item[FAILURE_ITEM_KEY] for item in response.json()[BATCH_FAILURES_KEY]} == {"msg-bad"}
