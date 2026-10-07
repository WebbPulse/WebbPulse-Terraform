"""Turning a run transition into queued deliveries, and sending one delivery.

The runs stream consumer enqueues one message per matching configuration, so a slow
or failing receiver never holds up a run or another receiver. The queue consumer
sends it and, on a fault a later attempt may not hit, sends it back to the queue with
a growing delay rather than raising, which keeps SQS's own redrive for the case where
the queue itself is unreachable. Every attempt's outcome is stamped on the
configuration as its last delivery.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Final, Mapping

import boto3
from webbpulse.dynamodb import now_iso
from webbpulse.identity.crypto import EnvelopeDecryptionFailed

from ..composition.settings import Settings, get_settings
from ..core.variable_cipher import MasterKeyUnavailable
from ..db import repositories
from . import payloads, sender, store
from .triggers import VERIFICATION, title_for

MESSAGE_KIND: Final = "run_notification"
"""The `kind` a delivery message carries on the runs function's queue router."""

MAX_ATTEMPTS: Final = 5

RETRY_DELAYS_SECONDS: Final = (30, 120, 600, 900)
"""The delay before the second to fifth attempts. 900 seconds is SQS's longest delay."""

MAX_DELAY_SECONDS: Final = 900

SECRET_UNAVAILABLE: Final = "secret_unavailable"
"""The error a delivery records when the stored URL cannot be opened, which no retry fixes."""

SUCCEEDED: Final = "succeeded"
FAILED: Final = "failed"
RETRYING: Final = "retrying"

QUEUE_SUFFIX: Final = "run-notifications"
"""The queue's name after the stack prefix. It is found by name rather than passed in,
since the runs function's environment is close to Lambda's 4 KB ceiling."""

SEND_BATCH: Final = 10
"""SQS's SendMessageBatch ceiling."""

_log = logging.getLogger(__name__)


def _sqs(settings: Settings) -> Any:
    """An SQS client in the stack's region."""
    return boto3.client("sqs", region_name=settings.AWS_REGION_NAME or None)


@functools.lru_cache(maxsize=8)
def _queue_url_by_name(name: str, region: str) -> str:
    """The URL of the queue with this name, looked up once per warm function."""
    client = boto3.client("sqs", region_name=region or None)
    return str(client.get_queue_url(QueueName=name)["QueueUrl"])


def queue_url(settings: Settings) -> str:
    """The notifications queue's URL: the configured one, else found from the stack prefix."""
    if settings.RUN_NOTIFICATIONS_QUEUE_URL:
        return settings.RUN_NOTIFICATIONS_QUEUE_URL
    prefix = settings.IDENTITY_TABLE_PREFIX.strip() or f"webbpulse-terraform-{settings.ENVIRONMENT}"
    return _queue_url_by_name(f"{prefix}-{QUEUE_SUFFIX}", settings.AWS_REGION_NAME)


def run_url(settings: Settings, workspace_id: str, run_id: str) -> str | None:
    """The SPA page for the run, or `None` while the environment has no frontend origin."""
    base = settings.IDENTITY_FRONTEND_BASE_URL.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/workspaces/{workspace_id}/runs/{run_id}"


def run_notification(
    configuration: Mapping[str, Any],
    workspace: Mapping[str, Any],
    run: Mapping[str, Any],
    *,
    trigger: str,
    status: str,
    updated_at: str | None,
    settings: Settings,
) -> payloads.Notification:
    """What a delivery about one run transition says.

    The status is the one the transition entered, not the run's current one, so a
    delivery retried after the run moved on still describes the event that fired it.
    """
    vcs = run.get("vcs") if isinstance(run.get("vcs"), Mapping) else {}
    sha = str(vcs.get("sha") or "")
    pr_number = vcs.get("pr_number")
    branch = str(vcs.get("branch") or "") or (f"pull request #{pr_number}" if pr_number else "")
    actor = payloads.actor_name(run.get("actor"))
    as_of = {**run, "status": status}
    return payloads.Notification(
        configuration_id=str(configuration["notification_id"]),
        configuration_name=str(configuration.get("name", "")),
        trigger=trigger,
        title=title_for(trigger, status),
        workspace_id=str(workspace["workspace_id"]),
        workspace_name=str(workspace.get("name", "")),
        run_id=str(run["run_id"]),
        run_url=run_url(settings, str(workspace["workspace_id"]), str(run["run_id"])),
        run_message=str(run.get("message") or "") or None,
        run_status=status,
        run_created_at=str(run.get("created_at") or "") or None,
        run_created_by=actor,
        run_updated_at=updated_at or str(run.get("updated_at") or "") or None,
        run_updated_by=actor,
        branch=branch or None,
        commit=sha[:8] or None,
        repository=str(vcs.get("repo") or "") or None,
        counts=payloads.counts_line(as_of),
    )


def verification_notification(configuration: Mapping[str, Any], workspace: Mapping[str, Any]) -> payloads.Notification:
    """The test delivery HCP's verify action sends: no run, trigger `verification`."""
    return payloads.Notification(
        configuration_id=str(configuration["notification_id"]),
        configuration_name=str(configuration.get("name", "")),
        trigger=VERIFICATION,
        title=f"Verification of {configuration.get('name', '')}",
        workspace_id=str(workspace["workspace_id"]),
        workspace_name=str(workspace.get("name", "")),
        run_updated_at=now_iso(),
    )


def send(
    configuration: Mapping[str, Any], notification: payloads.Notification, *, settings: Settings
) -> sender.SendResult:
    """Open the configuration's secrets, render the body and POST it."""
    try:
        url, token = store.secrets(configuration, settings=settings)
    except (EnvelopeDecryptionFailed, MasterKeyUnavailable):
        return sender.SendResult(status_code=None, error=SECRET_UNAVAILABLE)
    body, headers = payloads.render(str(configuration.get("destination_type", "")), notification, token)
    return sender.post(url, body, headers)


def delivery_record(
    result: sender.SendResult, *, status: str, trigger: str, run_id: str | None, attempts: int
) -> dict[str, Any]:
    """The `last_delivery` map one attempt leaves on its configuration."""
    record: dict[str, Any] = {
        "status": status,
        "trigger": trigger,
        "attempts": attempts,
        "attempted_at": now_iso(),
    }
    if run_id:
        record["run_id"] = run_id
    if result.status_code is not None:
        record["status_code"] = result.status_code
    if result.error:
        record["error"] = result.error
    if result.response_excerpt:
        record["response_excerpt"] = result.response_excerpt
    return record


def verify(
    configuration: Mapping[str, Any], workspace: Mapping[str, Any], *, settings: Settings | None = None
) -> dict[str, Any]:
    """Send the test delivery now and record it, returning the recorded outcome."""
    resolved = settings or get_settings()
    result = send(configuration, verification_notification(configuration, workspace), settings=resolved)
    record = delivery_record(
        result, status=SUCCEEDED if result.ok else FAILED, trigger=VERIFICATION, run_id=None, attempts=1
    )
    store.record_delivery(
        str(configuration["workspace_id"]), str(configuration["notification_id"]), record, settings=resolved
    )
    _log.info(
        "Sent a notification test.",
        extra={
            "event": "notifications.verify",
            "workspace_id": str(configuration["workspace_id"]),
            "notification_id": str(configuration["notification_id"]),
            "status_code": result.status_code,
            "error_category": result.error,
        },
    )
    return record


def messages_for(
    workspace_id: str, run_id: str, *, trigger: str, status: str, updated_at: str | None, settings: Settings
) -> list[dict[str, Any]]:
    """One delivery message per enabled configuration on the workspace subscribed to `trigger`."""
    return [
        {
            "kind": MESSAGE_KIND,
            "workspace_id": workspace_id,
            "notification_id": str(configuration["notification_id"]),
            "run_id": run_id,
            "trigger": trigger,
            "status": status,
            "updated_at": updated_at,
            "attempt": 1,
        }
        for configuration in store.matching_configurations(workspace_id, trigger, settings=settings)
    ]


def enqueue(messages: list[dict[str, Any]], *, settings: Settings, delay_seconds: int = 0) -> None:
    """Send delivery messages to the notifications queue, raising if any is refused."""
    if not messages:
        return
    url = queue_url(settings)
    client = _sqs(settings)
    for start in range(0, len(messages), SEND_BATCH):
        batch = messages[start : start + SEND_BATCH]
        response = client.send_message_batch(
            QueueUrl=url,
            Entries=[
                {"Id": str(index), "MessageBody": json.dumps(message), "DelaySeconds": delay_seconds}
                for index, message in enumerate(batch)
            ],
        )
        if response.get("Failed"):
            raise RuntimeError(f"{len(response['Failed'])} notification message(s) were refused by the queue")


def retry_delay(attempt: int, retry_after: int | None) -> int:
    """The delay before attempt `attempt + 1`: the schedule's, or longer if the receiver asked."""
    scheduled = RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)]
    if retry_after is not None:
        scheduled = max(scheduled, retry_after)
    return min(scheduled, MAX_DELAY_SECONDS)


def handle_message(message: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Deliver one queued notification, sending it back with a delay on a transient fault.

    A configuration deleted or disabled since the message was queued, or a run or
    workspace that is gone, ends the delivery quietly. Only a failure to requeue raises,
    so the queue's own retry and dead letter queue cover it.
    """
    resolved = settings or get_settings()
    workspace_id = str(message.get("workspace_id", ""))
    notification_id = str(message.get("notification_id", ""))
    run_id = str(message.get("run_id", ""))
    trigger = str(message.get("trigger", ""))
    status = str(message.get("status", ""))
    attempt = int(message.get("attempt", 1))
    try:
        configuration = store.get_configuration(workspace_id, notification_id, settings=resolved)
    except store.NotificationNotFound:
        return
    if not configuration.get("enabled", True) or trigger not in (configuration.get("triggers") or []):
        return
    workspace = repositories.workspaces(resolved).get({"workspace_id": workspace_id})
    run = repositories.runs(resolved).get({"run_id": run_id})
    if workspace is None or run is None:
        return
    notification = run_notification(
        configuration,
        workspace,
        run,
        trigger=trigger,
        status=status,
        updated_at=message.get("updated_at"),
        settings=resolved,
    )
    result = send(configuration, notification, settings=resolved)
    retry = not result.ok and result.retryable and attempt < MAX_ATTEMPTS
    outcome = SUCCEEDED if result.ok else RETRYING if retry else FAILED
    if retry:
        enqueue(
            [{**message, "attempt": attempt + 1}],
            settings=resolved,
            delay_seconds=retry_delay(attempt, result.retry_after),
        )
    store.record_delivery(
        workspace_id,
        notification_id,
        delivery_record(result, status=outcome, trigger=trigger, run_id=run_id, attempts=attempt),
        settings=resolved,
    )
    _log.info(
        "Delivered a run notification.",
        extra={
            "event": "notifications.delivery",
            "workspace_id": workspace_id,
            "notification_id": notification_id,
            "run_id": run_id,
            "trigger": trigger,
            "attempt": attempt,
            "outcome": outcome,
            "status_code": result.status_code,
            "error_category": result.error,
        },
    )


__all__ = [
    "MAX_ATTEMPTS",
    "MESSAGE_KIND",
    "RETRY_DELAYS_SECONDS",
    "enqueue",
    "handle_message",
    "messages_for",
    "queue_url",
    "retry_delay",
    "run_notification",
    "send",
    "verification_notification",
    "verify",
]
