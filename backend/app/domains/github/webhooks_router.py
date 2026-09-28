"""The route GitHub posts App webhook deliveries to.

It carries no authorizer and no admin guard. `WebhookSignatureMiddleware` wraps the
whole application and refuses any delivery the App's secret did not sign before this
module runs, so everything read here is GitHub's own signed payload. The route only
queues: the fetch, the ingest and the report happen on the runs function, and GitHub
gets its answer within its ten second window whatever the repository's size.

GitHub signs the body but no time, so a delivery whose event is older than
`MAX_DELIVERY_AGE`, or that carries no event time, is acknowledged and dropped:
within the window the consumers' dedupe makes a replay a no-op, past it nothing
genuine can arrive.

A push of a semantic version tag, and a published release, go to the registry's own
ingest queue instead, so module and provider publishing never pass through the runs
function or its VCS ingest.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request, Response, status

from ...common.composition.settings import Settings, get_settings
from ...common.github.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    PING,
    WEBHOOK_PATH,
    delivery_message,
    is_stale,
    release_message,
    tag_message,
)

_log = logging.getLogger(__name__)

MAX_MESSAGE_BYTES: Final = 200_000
"""Past this a message drops its changed paths, well inside SQS's 256 KiB."""

PULL_REQUEST_DELAY_SECONDS: Final = 5
"""How long a pull request waits on the queue, so GitHub has computed its merge commit."""

router = APIRouter(tags=["github"])


def _sqs(settings: Settings) -> Any:
    """An SQS client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("sqs", region_name=settings.AWS_REGION_NAME or None)


def enqueue(message: dict[str, Any], *, settings: Settings) -> None:
    """Send one delivery to the webhooks queue, shrinking it first when it is too large."""
    body = json.dumps(message, separators=(",", ":"))
    if len(body.encode()) > MAX_MESSAGE_BYTES:
        body = json.dumps({**message, "paths": None}, separators=(",", ":"))
    delay = PULL_REQUEST_DELAY_SECONDS if message["event"] == "pull_request" else 0
    _sqs(settings).send_message(QueueUrl=settings.GITHUB_WEBHOOKS_QUEUE_URL, MessageBody=body, DelaySeconds=delay)


def enqueue_tag(message: dict[str, Any], *, settings: Settings) -> None:
    """Send one tag push or release to the registry's ingest queue, or drop it where there is none.

    An environment without the registry acknowledges the delivery rather than
    failing it, since GitHub would only redeliver it to the same answer.
    """
    extra = {
        "event": f"github.webhook.{message['kind']}",
        "delivery": message["delivery"],
        "repository": message["repo"],
    }
    if not settings.REGISTRY_INGEST_QUEUE_URL:
        _log.info("Dropped a registry delivery with no registry here.", extra={**extra, "queued": False})
        return
    body = json.dumps({**message, "received_at_ms": int(time.time() * 1000)}, separators=(",", ":"))
    _sqs(settings).send_message(QueueUrl=settings.REGISTRY_INGEST_QUEUE_URL, MessageBody=body)
    _log.info("Queued a delivery for the registry.", extra={**extra, "queued": True, "tag": message["tag"]})


@router.post(WEBHOOK_PATH, status_code=status.HTTP_202_ACCEPTED, include_in_schema=False)
async def receive(request: Request) -> Response:
    """Queue one signed delivery the bridge acts on, and acknowledge the rest."""
    settings = get_settings()
    event = request.headers.get(EVENT_HEADER, "")
    delivery = request.headers.get(DELIVERY_HEADER, "")
    if event == PING:
        return Response(status_code=status.HTTP_200_OK)
    try:
        payload = json.loads(await request.body())
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail={"message": "The delivery is not JSON.", "error_code": "BAD_DELIVERY"}
        ) from error
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail={"message": "The delivery is not an object.", "error_code": "BAD_DELIVERY"}
        )
    extra = {"event": "github.webhook.received", "github_event": event, "delivery": delivery[:64]}
    tag = tag_message(event, delivery, payload) or release_message(event, delivery, payload)
    message = tag or delivery_message(event, delivery, payload)
    if message is None:
        _log.info("Acknowledged a delivery the bridge ignores.", extra={**extra, "queued": False})
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if is_stale(event, payload):
        _log.warning(
            "Dropped a delivery older than GitHub's redelivery window.",
            extra={**extra, "event": "github.webhook.stale", "queued": False},
        )
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if tag is not None:
        enqueue_tag(tag, settings=settings)
        return Response(status_code=status.HTTP_202_ACCEPTED)
    if not settings.GITHUB_WEBHOOKS_QUEUE_URL:
        _log.warning("No webhooks queue is configured.", extra={**extra, "queued": False})
        raise HTTPException(
            status_code=503, detail={"message": "Webhooks are not wired here.", "error_code": "WEBHOOKS_UNAVAILABLE"}
        )
    enqueue({**message, "received_at_ms": int(time.time() * 1000)}, settings=settings)
    _log.info("Queued a delivery.", extra={**extra, "queued": True, "repository": message["repo"]})
    return Response(status_code=status.HTTP_202_ACCEPTED)
