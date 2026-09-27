"""GitHub App webhooks: the signature gate in front of the route, and the queued message.

The webhook route has no authorizer, since GitHub cannot present one. What stands in
for it is `WebhookSignatureMiddleware`, the outermost layer of the application that
serves the route. It reads the raw body, checks `X-Hub-Signature-256` against the
`GITHUB_WEBHOOK_SECRET` key of the `app` secret, and answers 401 before any routing,
parsing or handler runs. With no secret configured every delivery is refused.

A verified delivery the bridge acts on becomes one compact message, built from the
signed payload alone. A branch push or a pull request goes to the webhooks queue
through `delivery_message`, and the runs function reads it back with
`parse_message`. A push of a semantic version tag goes to the registry's ingest
queue through `tag_message`, and the registry function reads it back with
`parse_tag_message`. Both sides live here so the functions share the shape without
importing each other.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, Optional

from webbpulse.http import SignatureMismatch, verify_hmac_signature
from webbpulse.integrations.github import GitHubNotConfigured

from ..composition.settings import Settings
from .loader import github_app_settings

_log = logging.getLogger(__name__)

WEBHOOK_PATH: Final = "/api/v1/github/webhooks"
"""Where GitHub posts deliveries, and the one path the signature gate guards."""

WEBHOOK_KIND: Final = "github_webhook"
"""The `kind` a queued delivery carries, which the runs function routes on."""

TAG_KIND: Final = "module_tag"
"""The `kind` a queued tag push carries, which the registry function routes on."""

SIGNATURE_HEADER: Final = "x-hub-signature-256"
EVENT_HEADER: Final = "x-github-event"
DELIVERY_HEADER: Final = "x-github-delivery"

MAX_BODY_BYTES: Final = 10 * 1024 * 1024
"""API Gateway's own payload ceiling, so nothing larger can arrive in production."""

MAX_CHANGED_PATHS: Final = 3000
"""More changed paths than this and the message says every path instead."""

PUSH: Final = "push"
PULL_REQUEST: Final = "pull_request"
PING: Final = "ping"

PULL_REQUEST_ACTIONS: Final = frozenset({"opened", "synchronize", "reopened"})
"""The pull request actions that change what a speculative plan would read."""

ZERO_SHA: Final = "0" * 40

_BRANCH_REF = re.compile(r"^refs/heads/(?P<branch>.+)$")
_SEMVER_TAG_REF = re.compile(
    r"^refs/tags/(?P<tag>v?(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?))$"
)
_DELIVERY = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class MalformedDelivery(Exception):
    """A queued delivery this consumer cannot read."""


def webhook_secret(settings: Settings) -> Optional[str]:
    """The webhook secret from the `app` secret, or `None` while there is none."""
    try:
        app = github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    except GitHubNotConfigured:
        return None
    if app.webhook_secret is None:
        return None
    value = app.webhook_secret.get_secret_value()
    return value or None


class WebhookSignatureMiddleware:
    """Refuse every request to the webhook path whose body the App's secret did not sign."""

    def __init__(self, app: ASGIApp, settings: Settings, path: str = WEBHOOK_PATH) -> None:
        """Wrap `app`, guarding `path` with the secret `settings` resolves."""
        self.app = app
        self.settings = settings
        self.path = path

    def _guarded(self, scope: Scope) -> bool:
        """Whether this request is one the gate must check."""
        if scope["type"] != "http":
            return False
        return str(scope.get("path", "")).rstrip("/") == self.path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Buffer the body, verify it, then replay it to the app or answer 401."""
        if not self._guarded(scope):
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                await _refuse(send, 413, "The delivery is too large.")
                return
            chunks.append(chunk)
            more = bool(message.get("more_body", False))
        body = b"".join(chunks)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        secret = webhook_secret(self.settings)
        try:
            if secret is None:
                raise SignatureMismatch("No webhook secret is configured.")
            verify_hmac_signature(body, headers.get(SIGNATURE_HEADER), secret)
        except SignatureMismatch:
            _log.warning(
                "Refused a webhook delivery with a bad signature.",
                extra={"event": "github.webhook.refused", "delivery": headers.get(DELIVERY_HEADER, "")[:64]},
            )
            await _refuse(send, 401, "The signature does not match.")
            return

        replayed = False

        async def replay() -> Message:
            """Hand the buffered body over once, then pass through to the real receive."""
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _refuse(send: Send, status: int, message: str) -> None:
    """Answer with this API's error envelope, without touching the app."""
    payload = json.dumps({"detail": {"message": message, "error_code": "GITHUB_WEBHOOK_REFUSED"}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _repository(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The repository fields every message carries."""
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}
    return {
        "repo": str(repository.get("full_name") or ""),
        "repository_id": str(repository.get("id") or ""),
        "installation_id": int(installation.get("id") or 0),
        "actor": str((payload.get("sender") or {}).get("login") or "github"),
    }


def _push_paths(payload: Mapping[str, Any]) -> Optional[list[str]]:
    """The paths a push changed, or `None` for every path.

    A new branch, a forced push and a push GitHub truncated compare against nothing
    the bridge can trust, so each of them means every path.
    """
    if payload.get("created") or payload.get("forced") or str(payload.get("before", "")) == ZERO_SHA:
        return None
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits or len(commits) >= 2048:
        return None
    paths: set[str] = set()
    for commit in commits:
        if not isinstance(commit, Mapping):
            return None
        for field in ("added", "removed", "modified"):
            paths.update(str(path) for path in commit.get(field) or [])
        if len(paths) > MAX_CHANGED_PATHS:
            return None
    return sorted(paths)


def delivery_message(event: str, delivery: str, payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The queued message for one verified delivery, or `None` when the bridge ignores it.

    A branch push that deleted nothing, and a pull request opened, reopened or pushed
    to from a branch of the same repository, are acted on. A pull request from a fork
    is ignored, because its speculative plan would run fork code under the
    workspace's run role.
    """
    if not _DELIVERY.match(delivery):
        return None
    base = {"kind": WEBHOOK_KIND, "delivery": delivery, "event": event, **_repository(payload)}
    if not base["repo"] or not base["repository_id"] or not base["installation_id"]:
        return None
    if event == PUSH:
        ref = str(payload.get("ref") or "")
        sha = str(payload.get("after") or "")
        if payload.get("deleted") or not _SHA.match(sha) or sha == ZERO_SHA:
            return None
        branch = _BRANCH_REF.match(ref)
        if branch is None:
            return None
        return {**base, "ref": ref, "sha": sha, "branch": branch.group("branch"), "paths": _push_paths(payload)}
    if event == PULL_REQUEST:
        if str(payload.get("action")) not in PULL_REQUEST_ACTIONS:
            return None
        pull = payload.get("pull_request") or {}
        head = pull.get("head") or {}
        base_ref = pull.get("base") or {}
        head_repository = (head.get("repo") or {}).get("id")
        if str(head_repository or "") != base["repository_id"]:
            _log.info(
                "Ignored a pull request from a fork.", extra={"event": "github.webhook.fork", "delivery": delivery}
            )
            return None
        number = pull.get("number") or payload.get("number")
        head_sha = str(head.get("sha") or "")
        if not isinstance(number, int) or number <= 0 or not _SHA.match(head_sha):
            return None
        return {
            **base,
            "ref": f"refs/pull/{number}/merge",
            "pr_number": number,
            "head_sha": head_sha,
            "base_sha": str(base_ref.get("sha") or "") or None,
            "base_branch": str(base_ref.get("ref") or "") or None,
        }
    return None


def tag_message(event: str, delivery: str, payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The registry message for a push of a semantic version tag, or `None` for anything else.

    `vX.Y.Z` and `X.Y.Z` both publish, a prerelease suffix included. A deleted tag
    publishes nothing and unpublishes nothing, and any other tag is ignored. The
    commit is the push's head commit, since for an annotated tag `after` names the
    tag object rather than the commit it points at.
    """
    if event != PUSH or not _DELIVERY.match(delivery) or payload.get("deleted"):
        return None
    match = _SEMVER_TAG_REF.match(str(payload.get("ref") or ""))
    if match is None:
        return None
    base = _repository(payload)
    if not base["repo"] or not base["repository_id"] or not base["installation_id"]:
        return None
    head = payload.get("head_commit") or {}
    sha = str(head.get("id") or "") if isinstance(head, Mapping) else ""
    if not _SHA.match(sha):
        sha = str(payload.get("after") or "")
    if not _SHA.match(sha) or sha == ZERO_SHA:
        return None
    return {
        "kind": TAG_KIND,
        "delivery": delivery,
        "event": event,
        **base,
        "ref": str(payload["ref"]),
        "tag": match.group("tag"),
        "version": match.group("version"),
        "sha": sha,
    }


def parse_tag_message(record: Mapping[str, Any]) -> dict[str, Any]:
    """The queued tag push one SQS record carries.

    Raises:
        MalformedDelivery: The body is not a `module_tag` message with every field
            it needs, so it parks on the dead letter queue.
    """
    raw = record.get("body")
    try:
        body = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise MalformedDelivery("The body is not JSON.") from error
    if not isinstance(body, dict) or body.get("kind") != TAG_KIND:
        raise MalformedDelivery(f"The body is not a {TAG_KIND} message.")
    required = ["delivery", "repo", "repository_id", "installation_id", "tag", "version", "sha", "actor"]
    missing = [name for name in required if not body.get(name)]
    if missing:
        raise MalformedDelivery(f"The message lacks {', '.join(missing)}.")
    if not _SHA.match(str(body["sha"])):
        raise MalformedDelivery("The message names no commit.")
    return body


def parse_message(record: Mapping[str, Any]) -> dict[str, Any]:
    """The queued delivery one SQS record carries.

    Raises:
        MalformedDelivery: The body is not a `github_webhook` message with the fields
            its event needs, so it parks on the dead letter queue.
    """
    raw = record.get("body")
    try:
        body = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise MalformedDelivery("The body is not JSON.") from error
    if not isinstance(body, dict) or body.get("kind") != WEBHOOK_KIND:
        raise MalformedDelivery("The body is not a github_webhook message.")
    required = ["delivery", "event", "repo", "repository_id", "installation_id", "ref", "actor"]
    if body.get("event") == PUSH:
        required += ["sha", "branch"]
    elif body.get("event") == PULL_REQUEST:
        required += ["pr_number", "head_sha"]
    else:
        raise MalformedDelivery("The message names no event the bridge handles.")
    missing = [name for name in required if not body.get(name)]
    if missing:
        raise MalformedDelivery(f"The message lacks {', '.join(missing)}.")
    return body


__all__ = [
    "DELIVERY_HEADER",
    "EVENT_HEADER",
    "MAX_CHANGED_PATHS",
    "PING",
    "PULL_REQUEST",
    "PUSH",
    "SIGNATURE_HEADER",
    "TAG_KIND",
    "WEBHOOK_KIND",
    "WEBHOOK_PATH",
    "MalformedDelivery",
    "WebhookSignatureMiddleware",
    "delivery_message",
    "parse_message",
    "parse_tag_message",
    "tag_message",
    "webhook_secret",
]
