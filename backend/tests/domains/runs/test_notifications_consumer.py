"""Run notifications: stream transitions queued as deliveries, and each delivery sent with retries."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import httpx
import pytest
from boto3.dynamodb.types import TypeSerializer

from app.common.composition import settings as settings_module
from app.common.db import repositories
from app.common.notifications import delivery, payloads, sender, store
from app.common.notifications.triggers import trigger_for
from app.domains.runs.consumers import dispatch, notifications
from tests.conftest import REGION, TABLE_PREFIX

WORKSPACE_ID = "ws-01ARZ3NDEKTSV4RRFFQ69G5FAV"
RUN_ID = "run-01ARZ3NDEKTSV4RRFFQ69G5FAW"
GENERIC_URL = "https://receiver.example.com/hook"
QUEUE_NAME = f"{TABLE_PREFIX}-run-notifications"


def _image(values: dict[str, Any]) -> dict[str, Any]:
    """A DynamoDB stream image of `values`."""
    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in values.items()}


def _record(new: dict[str, Any], old: dict[str, Any] | None = None, event: str = "MODIFY") -> dict[str, Any]:
    """The stream record one runs table write produces."""
    body: dict[str, Any] = {"NewImage": _image(new), "SequenceNumber": "1"}
    if old is not None:
        body["OldImage"] = _image(old)
    return {"eventID": "1", "eventName": event, "eventSource": "aws:dynamodb", "dynamodb": body}


def _run(status: str, **extra: Any) -> dict[str, Any]:
    """A run row as the runs table holds it."""
    return {
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "collection": "run",
        "status": status,
        "message": "Queued manually",
        "created_at": "2026-10-07T10:00:00Z",
        "updated_at": "2026-10-07T10:01:00Z",
        "source": "tfe_api",
        "vcs": {"repo": "WebbPulse/example", "sha": "0123456789abcdef", "branch": "main"},
        "actor": {"kind": "user", "id": "user-1", "display_name": "Tyler"},
        "changes": {"add": 2, "change": 1, "destroy": 0},
        **extra,
    }


@pytest.fixture
def queue() -> Iterator[str]:
    """The notifications queue, found by name as the deployed function finds it."""
    delivery._queue_url_by_name.cache_clear()
    url = boto3.client("sqs", region_name=REGION).create_queue(QueueName=QUEUE_NAME)["QueueUrl"]
    yield url
    delivery._queue_url_by_name.cache_clear()


def _messages(queue_url: str) -> list[dict[str, Any]]:
    """Every message waiting on the queue, bodies parsed."""
    client = boto3.client("sqs", region_name=REGION)
    found: list[dict[str, Any]] = []
    while True:
        response = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        batch = response.get("Messages", [])
        if not batch:
            return found
        for message in batch:
            found.append(json.loads(message["Body"]))
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def _configuration(**overrides: Any) -> dict[str, Any]:
    """A stored generic configuration on the test workspace."""
    payload = {
        "name": "Receiver",
        "destination_type": "generic",
        "url": GENERIC_URL,
        "token": "signing-token",
        "triggers": ["run:created", "run:needs_attention", "run:completed", "run:errored"],
        **overrides,
    }
    return store.create_configuration(WORKSPACE_ID, payload)


def _seed(status: str = "applied", **extra: Any) -> dict[str, Any]:
    """Store the workspace and the run a delivery reads."""
    settings = settings_module.get_settings()
    repositories.workspaces(settings).put({"workspace_id": WORKSPACE_ID, "name": "example"})
    run = _run(status, **extra)
    repositories.runs(settings).put(run)
    return run


class Receiver:
    """An in-test receiver answering each request with the next scripted status."""

    def __init__(self, *statuses: int, headers: dict[str, str] | None = None) -> None:
        """Answer with `statuses` in turn, then 200."""
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record and answer one request."""
        self.requests.append(request)
        code = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(code, text="ok" if code < 300 else "busy", headers=self.headers)


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Receiver:
    """A receiver every delivery reaches through a public address."""
    target = Receiver()
    monkeypatch.setattr(sender, "resolve", lambda host, port: ["93.184.216.34"])
    monkeypatch.setattr(sender, "_transport", lambda: httpx.MockTransport(target))
    return target


@pytest.mark.parametrize(
    ("event", "old", "new", "expected"),
    [
        ("INSERT", None, "pending", "run:created"),
        ("MODIFY", "pending", "planning", "run:planning"),
        ("MODIFY", "planning", "awaiting_confirmation", "run:needs_attention"),
        ("MODIFY", "confirmed", "applying", "run:applying"),
        ("MODIFY", "applying", "applied", "run:completed"),
        ("MODIFY", "planning", "planned_and_finished", "run:completed"),
        ("MODIFY", "awaiting_confirmation", "discarded", "run:completed"),
        ("MODIFY", "planning", "errored", "run:errored"),
        ("MODIFY", "pending", "cancelled", "run:errored"),
        ("MODIFY", "applied", "applied", None),
        ("MODIFY", "planned", "confirmed", None),
        ("REMOVE", "applied", "applied", None),
    ],
)
def test_each_transition_fires_hcps_trigger(event, old, new, expected):
    """The trigger table, as HCP maps run states."""
    assert trigger_for(event, old, new) == expected


def test_the_semaphore_row_fires_nothing():
    """Only run rows carry the run collection."""
    record = _record({"run_id": "semaphore#ws", "workspace_id": WORKSPACE_ID, "status": "held"}, event="INSERT")

    assert notifications.notification_event(record) is None


def test_a_write_that_keeps_the_status_fires_nothing():
    """A heartbeat or a field stamp during planning is not a second notification."""
    record = _record(_run("planning", heartbeat_at="x"), _run("planning"))

    assert notifications.notification_event(record) is None


def test_a_transition_queues_one_message_per_matching_configuration(queue):
    """Enabled configurations subscribed to the trigger each get a message; others none."""
    wanted = _configuration()
    _configuration(name="Disabled", enabled=False)
    _configuration(name="Other trigger", triggers=["run:planning"])

    dispatch.route_record(_record(_run("awaiting_confirmation"), _run("planning")))

    [message] = _messages(queue)
    assert message == {
        "kind": delivery.MESSAGE_KIND,
        "workspace_id": WORKSPACE_ID,
        "notification_id": wanted["notification_id"],
        "run_id": RUN_ID,
        "trigger": "run:needs_attention",
        "status": "awaiting_confirmation",
        "updated_at": "2026-10-07T10:01:00Z",
        "attempt": 1,
    }


def test_queueing_never_raises(monkeypatch):
    """A queue that cannot be reached loses the notification, never the stream batch."""
    _configuration()

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("queue down")

    monkeypatch.setattr(delivery, "enqueue", refuse)

    notifications.handle_record(_record(_run("pending"), event="INSERT"))


def test_a_delivery_is_sent_signed_and_recorded(queue, receiver):
    """The queued message becomes HCP's payload for the run, and the outcome lands on the configuration."""
    _seed("applied", apply_changes={"add": 2, "change": 1, "destroy": 0})
    configuration = _configuration()

    dispatch.route_record(
        {
            "body": json.dumps(
                {
                    "kind": delivery.MESSAGE_KIND,
                    "workspace_id": WORKSPACE_ID,
                    "notification_id": configuration["notification_id"],
                    "run_id": RUN_ID,
                    "trigger": "run:completed",
                    "status": "applied",
                    "updated_at": "2026-10-07T10:05:00Z",
                    "attempt": 1,
                }
            )
        }
    )

    [request] = receiver.requests
    body = json.loads(request.content)
    assert request.headers["x-tfe-notification-signature"] == payloads.sign(request.content, "signing-token")
    assert body["run_id"] == RUN_ID
    assert body["run_message"] == "Queued manually"
    assert body["run_created_by"] == "Tyler"
    assert body["workspace_name"] == "example"
    assert body["organization_name"] == "webbpulse"
    assert body["notifications"] == [
        {
            "message": "Run Applied",
            "trigger": "run:completed",
            "run_status": "applied",
            "run_updated_at": "2026-10-07T10:05:00Z",
            "run_updated_by": "Tyler",
        }
    ]
    stored = store.render(store.get_configuration(WORKSPACE_ID, configuration["notification_id"]))
    assert stored["last_delivery"]["status"] == "succeeded"
    assert stored["last_delivery"]["run_id"] == RUN_ID
    assert stored["last_delivery"]["trigger"] == "run:completed"
    assert _messages(queue) == []


def _message(configuration: dict[str, Any], attempt: int = 1) -> dict[str, Any]:
    """A queued delivery for the seeded run."""
    return {
        "kind": delivery.MESSAGE_KIND,
        "workspace_id": WORKSPACE_ID,
        "notification_id": configuration["notification_id"],
        "run_id": RUN_ID,
        "trigger": "run:completed",
        "status": "applied",
        "updated_at": None,
        "attempt": attempt,
    }


def test_a_transient_failure_is_requeued_with_backoff(queue, receiver, monkeypatch):
    """A 503 goes back on the queue with the next attempt and the scheduled delay."""
    _seed()
    configuration = _configuration()
    receiver.statuses = [503]
    sent: list[tuple[list[dict[str, Any]], int]] = []
    monkeypatch.setattr(
        delivery, "enqueue", lambda messages, *, settings, delay_seconds=0: sent.append((messages, delay_seconds))
    )

    delivery.handle_message(_message(configuration))

    [(messages, delay)] = sent
    assert messages[0]["attempt"] == 2
    assert delay == delivery.RETRY_DELAYS_SECONDS[0]
    last = store.render(store.get_configuration(WORKSPACE_ID, configuration["notification_id"]))["last_delivery"]
    assert last["status"] == "retrying"
    assert last["status_code"] == 503
    assert last["attempts"] == 1


def test_a_receivers_retry_after_lengthens_the_delay_up_to_the_cap():
    """Retry-After is honoured but never past SQS's longest delay."""
    assert delivery.retry_delay(1, None) == 30
    assert delivery.retry_delay(1, 300) == 300
    assert delivery.retry_delay(4, None) == 900
    assert delivery.retry_delay(2, 86400) == delivery.MAX_DELAY_SECONDS


def test_the_last_attempt_is_recorded_as_failed(queue, receiver):
    """After the fifth attempt the delivery stops and says so."""
    _seed()
    configuration = _configuration()
    receiver.statuses = [503]

    delivery.handle_message(_message(configuration, attempt=delivery.MAX_ATTEMPTS))

    last = store.render(store.get_configuration(WORKSPACE_ID, configuration["notification_id"]))["last_delivery"]
    assert last["status"] == "failed"
    assert last["attempts"] == delivery.MAX_ATTEMPTS
    assert _messages(queue) == []


def test_a_permanent_refusal_is_not_retried(queue, receiver):
    """A 404 will not change on retry."""
    _seed()
    configuration = _configuration()
    receiver.statuses = [404]

    delivery.handle_message(_message(configuration))

    assert _messages(queue) == []
    last = store.render(store.get_configuration(WORKSPACE_ID, configuration["notification_id"]))["last_delivery"]
    assert last["status"] == "failed"


def test_a_configuration_disabled_since_queueing_sends_nothing(queue, receiver):
    """The message is dropped when the configuration no longer wants it."""
    _seed()
    configuration = _configuration()
    store.update_configuration(WORKSPACE_ID, configuration["notification_id"], {"enabled": False})

    delivery.handle_message(_message(configuration))

    assert receiver.requests == []


def test_a_deleted_configuration_sends_nothing(queue, receiver):
    """A delete between queueing and delivery ends the delivery quietly."""
    _seed()
    configuration = _configuration()
    store.delete_configuration(WORKSPACE_ID, configuration["notification_id"])

    delivery.handle_message(_message(configuration))

    assert receiver.requests == []


def _notification(**overrides: Any) -> payloads.Notification:
    """A notification about the seeded run."""
    values: dict[str, Any] = {
        "configuration_id": "nc-1",
        "configuration_name": "Team",
        "trigger": "run:needs_attention",
        "title": "Run Needs Attention",
        "workspace_id": WORKSPACE_ID,
        "workspace_name": "prod <infra>",
        "run_id": RUN_ID,
        "run_url": f"https://terraform.example.com/workspaces/{WORKSPACE_ID}/runs/{RUN_ID}",
        "run_message": "Fix & ship",
        "run_status": "awaiting_confirmation",
        "branch": "main",
        "commit": "01234567",
        "counts": "Terraform plan: 2 to add, 1 to change, 0 to destroy.",
        "run_created_by": "Tyler",
        **overrides,
    }
    return payloads.Notification(**values)


def test_slack_gets_a_linked_escaped_message():
    """The run link, workspace, source and counts, with Slack's control characters escaped."""
    body = payloads.slack_body(_notification())

    text = body["blocks"][0]["text"]["text"]
    assert f"<https://terraform.example.com/workspaces/{WORKSPACE_ID}/runs/{RUN_ID}|{RUN_ID}>" in text
    assert "prod &lt;infra&gt;" in text
    assert "Fix &amp; ship" in text
    assert "main @ 01234567" in text
    assert "2 to add, 1 to change, 0 to destroy" in text
    assert body["text"] == "Run Needs Attention in prod <infra>"


def test_discord_gets_one_embed_with_no_mentions():
    """One embed linking the run, with the fields a reader needs and every mention off."""
    body = payloads.discord_body(_notification(run_message="@everyone " + "x" * 5000))

    [embed] = body["embeds"]
    assert body["allowed_mentions"] == {"parse": []}
    assert embed["url"].endswith(RUN_ID)
    assert len(embed["description"]) == payloads.DISCORD_DESCRIPTION_LIMIT
    names = [field["name"] for field in embed["fields"]]
    assert names == ["Workspace", "Run", "Branch", "Commit", "Plan", "Created by"]


def test_counts_are_worded_as_hcp_words_them():
    """No plan yet says nothing; an empty plan says so; an apply reports what it did."""
    assert payloads.counts_line({"status": "planning"}) is None
    assert payloads.counts_line({"status": "planned", "changes": {"add": 0, "change": 0, "destroy": 0}}) == (
        "Terraform plan: no changes."
    )
    assert payloads.counts_line(
        {"status": "applied", "changes": {"add": 1}, "apply_changes": {"add": 1, "change": 0, "destroy": 2}}
    ) == ("Apply: 1 added, 0 changed, 2 destroyed.")


def test_only_a_generic_webhook_is_signed():
    """Slack and Discord carry no signature header."""
    assert payloads.render("slack", _notification(), "token")[1] == {}
    _, headers = payloads.render("generic", _notification(), "token")
    assert set(headers) == {payloads.SIGNATURE_HEADER}
