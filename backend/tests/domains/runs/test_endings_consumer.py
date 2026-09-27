"""The endings consumer: runs the state machine ended, settled from the runs table's stream.

`MarkErrored` and its siblings write a terminal status straight to the table, so these
tests write the row the same way and then hand the stream record the write produces to
the events route, expecting everything `finish_run` does for an ending the API wrote.
"""

import logging
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer
from fastapi.testclient import TestClient
from webbpulse.events import BATCH_FAILURES_KEY, FAILURE_ITEM_KEY, events_path

from app.common.composition import settings as settings_module
from app.common.composition.wiring import build_domain_app
from app.common.db import repositories
from app.common.workspaces import cleanup
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import dispatch, endings

BASE = "/api/v1/runs"


def _mark(run_id: str, status: str, error: str | None = None) -> dict[str, Any]:
    """Write a terminal status as the state machine's DynamoDB task does, returning the new row."""
    expression = "SET #status = :status"
    values: dict[str, Any] = {":status": status}
    if error is not None:
        expression += ", #error = if_not_exists(#error, :error)"
        values[":error"] = error
    names = {"#status": "status"} | ({"#error": "error"} if error is not None else {})
    repositories.runs(settings_module.get_settings()).update(
        {"run_id": run_id},
        update_expression=expression + " REMOVE confirm_task_token",
        expression_names=names,
        expression_values=values,
    )
    stored = repositories.runs(settings_module.get_settings()).get({"run_id": run_id})
    assert stored is not None
    return dict(stored)


def _stream_record(new: dict[str, Any], event: str = "MODIFY", sequence: str = "1") -> dict[str, Any]:
    """The DynamoDB Streams record a write of `new` produces."""
    serializer = TypeSerializer()
    image = {key: serializer.serialize(value) for key, value in new.items()}
    return {
        "eventID": sequence,
        "eventName": event,
        "eventSource": "aws:dynamodb",
        "dynamodb": {"NewImage": image, "SequenceNumber": sequence},
    }


@pytest.fixture
def events_client(settings):
    """A client over the runs function, where the events route lives."""
    with TestClient(build_domain_app("runs", settings=settings_module.get_settings())) as client:
        yield client


def test_only_a_terminal_status_without_an_end_time_settles():
    """The same test the mapping's filter applies, repeated in code."""
    ended = {"run_id": "run-1", "status": "errored"}
    assert endings.settleable_run_id(_stream_record(ended)) == "run-1"
    assert endings.settleable_run_id(_stream_record(ended | {"finished_at": "2026-09-26T00:00:00Z"})) is None
    assert endings.settleable_run_id(_stream_record({"run_id": "run-1", "status": "planning"})) is None
    assert endings.settleable_run_id(_stream_record(ended, event="INSERT")) is None


def test_an_errored_ending_is_settled(app, auth_client, created_run, workspace, uploaded_config_version, events_client):
    """The token is revoked, the end time stamped and the run queued behind it started."""
    run_id = created_run["run_id"]
    queued = auth_client.post(
        BASE,
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": True,
        },
    ).json()
    assert queued["queued_behind"] == run_id

    row = _mark(run_id, "errored", "The run failed with InitFailed.")
    response = events_client.post(events_path(), json={"Records": [_stream_record(row)]})

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []
    settled = runs_service.get_run(run_id)
    assert settled["status"] == "errored"
    assert settled["finished_at"]
    promoted = runs_service.get_run(queued["run_id"])
    assert promoted["status"] == "planning"
    assert promoted.get("queued_behind") is None
    with TestClient(app, headers={"Authorization": f"Bearer {created_run['run_token']}"}) as runner:
        assert runner.get(f"{BASE}/{run_id}/bundle").status_code == 401


@pytest.mark.parametrize("status", ["applied", "planned_and_finished"])
def test_a_successful_ending_the_state_machine_wrote_is_settled(app, created_run, status):
    """A success whose phase result never arrived through the API still revokes the token."""
    run_id = created_run["run_id"]
    row = _mark(run_id, status)
    assert endings.handle_record(_stream_record(row), settings=settings_module.get_settings()) is True
    assert runs_service.get_run(run_id)["finished_at"]
    with TestClient(app, headers={"Authorization": f"Bearer {created_run['run_token']}"}) as runner:
        assert runner.get(f"{BASE}/{run_id}/bundle").status_code == 401


def test_settling_twice_keeps_the_first_end_time(created_run):
    """A redelivered record changes nothing the first delivery wrote."""
    run_id = created_run["run_id"]
    _mark(run_id, "errored", "The run failed with InitFailed.")
    first = runs_service.settle_run(run_id)
    second = runs_service.settle_run(run_id)
    assert first is not None and second is not None
    assert second["finished_at"] == first["finished_at"]


def test_a_run_still_going_is_not_settled(created_run):
    """Only a terminal run is touched, whatever the record claimed."""
    assert runs_service.settle_run(created_run["run_id"]) is None
    assert runs_service.get_run(created_run["run_id"]).get("finished_at") is None


def test_an_ending_the_api_wrote_is_left_to_finish_run(created_run):
    """A run `finish_run` ended already carries its end time, so the stream record is dropped."""
    run_id = created_run["run_id"]
    ended = runs_service.finish_run(run_id, "errored", error="The plan phase exited 1.")
    assert endings.handle_record(_stream_record(ended), settings=settings_module.get_settings()) is False


def test_a_settle_that_fails_is_retried(created_run, events_client, monkeypatch, caplog):
    """A fault raises through the route, so the mapping retries the record, and is logged as an error."""
    row = _mark(created_run["run_id"], "errored", "The run failed with InitFailed.")

    def broken(*_args: Any, **_kwargs: Any) -> None:
        """Fail the way a throttled table would."""
        raise RuntimeError("throttled")

    monkeypatch.setattr(runs_service, "_revoke_run_token", broken)
    with caplog.at_level(logging.INFO):
        response = events_client.post(events_path(), json={"Records": [_stream_record(row, sequence="42")]})

    assert [item[FAILURE_ITEM_KEY] for item in response.json()[BATCH_FAILURES_KEY]] == ["42"]
    failed = [record for record in caplog.records if getattr(record, "event", "") == "runs.events.batch.record_failed"]
    assert failed and failed[0].levelno == logging.ERROR and failed[0].exc_info


def test_a_cleanup_waiting_on_its_workspace_is_retried_quietly(monkeypatch, caplog):
    """The expected "not yet" of a cleanup is retried but logged at info without a traceback."""

    def waiting(*_args: Any, **_kwargs: Any) -> None:
        """Answer as a cleanup whose workspace delete has not finished does."""
        raise cleanup.WorkspaceStillPresent("ws-1")

    monkeypatch.setattr(dispatch, "route_record", waiting)
    with caplog.at_level(logging.INFO):
        envelope = dispatch.consume_batch({"Records": [{"messageId": "msg-1", "body": "{}"}]})

    assert [item[FAILURE_ITEM_KEY] for item in envelope[BATCH_FAILURES_KEY]] == ["msg-1"]
    deferred = [r for r in caplog.records if getattr(r, "event", "") == "runs.events.batch.record_deferred"]
    assert len(deferred) == 1
    assert deferred[0].levelno == logging.INFO
    assert deferred[0].exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
