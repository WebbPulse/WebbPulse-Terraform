"""Purging a deleted workspace's objects: run artifacts, config tarballs and state history.

A workspace delete removes its rows in the request and hands the objects here, because
listing and deleting every version under every run's prefix grows with the workspace's
history and can outlast the API's time budget. The delete sends `workspace_cleanup`
messages to a queue the runs function consumes; with no queue configured, as locally,
it calls `purge` inline instead.

The messages are sent before any row is removed, so the run ids they carry cannot be
lost to a delete that fails part way. The consumer therefore refuses to purge while the
workspace row still exists: a delete that failed leaves its workspace and state intact,
the message retries until a later delete finishes the job, and one that never does
parks on the dead letter queue having removed nothing.

Every object version and delete marker under a prefix is removed, not just the current
object, since the buckets are versioned and a plain delete would only hide the history.
Each step is idempotent, so a redelivered or continued message repeats no harm.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Final, Mapping, Sequence

from ..composition.settings import Settings, get_settings
from ..db import repositories

_log = logging.getLogger(__name__)

CLEANUP_KIND: Final = "workspace_cleanup"
"""The `kind` a cleanup message carries, which the runs function routes on."""

RUN_IDS_PER_MESSAGE: Final = 1000
"""The most run ids one message carries, well under the SQS body limit."""

DELETE_BATCH: Final = 1000
"""The most keys one `DeleteObjects` call accepts."""

CONSUMER_BUDGET_SECONDS: Final = 20.0
"""How long one delivery purges before it hands the rest to a continuation message,
leaving headroom under the function's thirty second timeout."""


class MalformedCleanup(Exception):
    """The message body is not a cleanup this consumer can act on."""


class WorkspaceStillPresent(Exception):
    """The workspace row still exists, so its delete has not finished and nothing is purged."""


class PurgeFailed(Exception):
    """S3 refused to delete some object versions."""


def run_prefix(run_id: str) -> str:
    """The artifacts bucket prefix every object of one run sits under."""
    return f"runs/{run_id}/"


def config_prefix(workspace_id: str) -> str:
    """The artifacts bucket prefix every config tarball of one workspace sits under."""
    return f"configs/{workspace_id}/"


def state_prefix(workspace_id: str) -> str:
    """The state bucket prefix holding the workspace's state object and its lock."""
    return f"workspaces/{workspace_id}/"


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def _sqs(settings: Settings) -> Any:
    """An SQS client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("sqs", region_name=settings.AWS_REGION_NAME or None)


def purge_prefix(bucket: str, prefix: str, *, settings: Settings) -> int:
    """Delete every object version and delete marker under `prefix`, returning how many went.

    An empty bucket name is a deployment with no such bucket, which has nothing to purge.
    """
    if not bucket:
        return 0
    client = _s3(settings)
    removed = 0
    for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        targets = [
            {"Key": str(item["Key"]), "VersionId": str(item["VersionId"])}
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
        ]
        for start in range(0, len(targets), DELETE_BATCH):
            batch = targets[start : start + DELETE_BATCH]
            response = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            errors = response.get("Errors", [])
            if errors:
                codes = sorted({str(error.get("Code", "")) for error in errors})
                raise PurgeFailed(f"{len(errors)} object versions under {prefix} could not be deleted: {codes}")
            removed += len(batch)
    return removed


def purge(
    workspace_id: str,
    run_ids: Sequence[str],
    *,
    settings: Settings | None = None,
    deadline: float | None = None,
) -> list[str] | None:
    """Purge each run's artifacts, then the config tarballs, then the state history.

    Returns None once everything is gone. When `deadline`, a `time.monotonic` value,
    passes between runs, returns the run ids still to purge so the caller can continue
    in a fresh invocation. At least one run is purged per call, so a continuation
    always makes progress.
    """
    resolved = settings or get_settings()
    ids = list(run_ids)
    for index, run_id in enumerate(ids):
        if index and deadline is not None and time.monotonic() >= deadline:
            return ids[index:]
        purge_prefix(resolved.ARTIFACTS_BUCKET, run_prefix(run_id), settings=resolved)
    purge_prefix(resolved.ARTIFACTS_BUCKET, config_prefix(workspace_id), settings=resolved)
    purge_prefix(resolved.STATE_BUCKET, state_prefix(workspace_id), settings=resolved)
    return None


def messages(workspace_id: str, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    """The cleanup message bodies for one workspace, its run ids split across as many as needed.

    Always at least one, since the workspace's own prefixes need purging even with no runs.
    """
    ids = list(run_ids)
    chunks = [ids[start : start + RUN_IDS_PER_MESSAGE] for start in range(0, len(ids), RUN_IDS_PER_MESSAGE)] or [[]]
    return [{"kind": CLEANUP_KIND, "workspace_id": workspace_id, "run_ids": chunk} for chunk in chunks]


def enqueue(workspace_id: str, run_ids: Sequence[str], *, settings: Settings | None = None) -> bool:
    """Send the cleanup messages, or return False when no queue is configured."""
    resolved = settings or get_settings()
    queue_url = resolved.WORKSPACE_CLEANUP_QUEUE_URL
    if not queue_url:
        return False
    client = _sqs(resolved)
    for body in messages(workspace_id, run_ids):
        client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
    return True


def _parse(record: Mapping[str, Any]) -> tuple[str, list[str]]:
    """The workspace id and run ids one record's body names, or `MalformedCleanup`."""
    try:
        body = json.loads(str(record.get("body", "")))
    except ValueError as error:
        raise MalformedCleanup("The cleanup body is not JSON.") from error
    if not isinstance(body, Mapping) or body.get("kind") != CLEANUP_KIND:
        raise MalformedCleanup("The body is not a workspace cleanup.")
    workspace_id = body.get("workspace_id")
    run_ids = body.get("run_ids", [])
    if not isinstance(workspace_id, str) or not workspace_id.startswith("ws-"):
        raise MalformedCleanup("The cleanup names no workspace.")
    if not isinstance(run_ids, list) or not all(isinstance(item, str) and item for item in run_ids):
        raise MalformedCleanup("The cleanup's run ids are not a list of ids.")
    return workspace_id, [str(item) for item in run_ids]


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Purge what one cleanup message names, continuing in a new message if time runs out.

    Raises:
        MalformedCleanup: The body cannot be acted on, so it parks on the dead letter queue.
        WorkspaceStillPresent: The delete has not finished, so the message is retried.
    """
    resolved = settings or get_settings()
    workspace_id, run_ids = _parse(record)
    if repositories.workspaces(resolved).get({"workspace_id": workspace_id}) is not None:
        raise WorkspaceStillPresent(workspace_id)
    remaining = purge(
        workspace_id,
        run_ids,
        settings=resolved,
        deadline=time.monotonic() + CONSUMER_BUDGET_SECONDS,
    )
    if remaining is None:
        _log.info(
            "Purged a deleted workspace's objects.",
            extra={"event": "workspaces.cleanup.done", "workspace_id": workspace_id, "runs": len(run_ids)},
        )
        return
    enqueue(workspace_id, remaining, settings=resolved)
    _log.info(
        "Continued a deleted workspace's purge in a new message.",
        extra={"event": "workspaces.cleanup.continued", "workspace_id": workspace_id, "remaining": len(remaining)},
    )


__all__ = [
    "CLEANUP_KIND",
    "MalformedCleanup",
    "PurgeFailed",
    "WorkspaceStillPresent",
    "enqueue",
    "handle_record",
    "messages",
    "purge",
    "purge_prefix",
]
