"""The webhook route: the signature gate in front of it and the message it queues.

Deliveries are signed with a known secret the way GitHub signs them, and the queue
is moto's, so what is under test is what a real delivery produces: a refusal before
any handler runs, an acknowledgement, or exactly one compact message.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from app.common.composition import settings as settings_module
from app.common.github import loader
from app.common.github.webhooks import WEBHOOK_KIND, WEBHOOK_PATH, delivery_message, parse_message
from app.domains.github import webhooks_router
from tests.domains.github.conftest import APP_ID

SECRET = "test-webhook-secret"
REPO = "WebbPulse/example-infra"
REPOSITORY_ID = 424242
INSTALLATION_ID = 777
SHA = "a" * 40
BEFORE = "b" * 40


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> Iterator[str]:
    """A webhooks queue and an App whose webhook secret is `SECRET`, wired into the settings."""
    url = boto3.client("sqs", region_name="us-west-2").create_queue(QueueName="webhooks")["QueueUrl"]
    monkeypatch.setenv("GITHUB_WEBHOOKS_QUEUE_URL", url)
    monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", private_key_pem)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhooks_router, "PULL_REQUEST_DELAY_SECONDS", 0)
    settings_module.reset_settings_cache()
    loader.invalidate()
    yield url
    loader.invalidate()
    settings_module.reset_settings_cache()


def queued(url: str) -> list[dict[str, Any]]:
    """Every message on the queue, with any delay ignored."""
    client = boto3.client("sqs", region_name="us-west-2")
    messages = client.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(message["Body"]) for message in messages]


def push(**overrides: Any) -> dict[str, Any]:
    """A push payload to main from a repository the App is installed on."""
    body = {
        "ref": "refs/heads/main",
        "before": BEFORE,
        "after": SHA,
        "created": False,
        "deleted": False,
        "forced": False,
        "commits": [
            {"added": ["infra/new.tf"], "removed": [], "modified": ["infra/main.tf"]},
            {"added": [], "removed": ["old.tf"], "modified": ["infra/main.tf"]},
        ],
        "repository": {"id": REPOSITORY_ID, "full_name": REPO},
        "installation": {"id": INSTALLATION_ID},
        "sender": {"login": "octocat"},
    }
    return body | overrides


def pull_request(action: str = "opened", head_repository: int = REPOSITORY_ID) -> dict[str, Any]:
    """A pull request payload from a branch of `head_repository`."""
    return {
        "action": action,
        "number": 7,
        "pull_request": {
            "number": 7,
            "head": {"sha": SHA, "ref": "feature", "repo": {"id": head_repository}},
            "base": {"sha": BEFORE, "ref": "main", "repo": {"id": REPOSITORY_ID}},
        },
        "repository": {"id": REPOSITORY_ID, "full_name": REPO},
        "installation": {"id": INSTALLATION_ID},
        "sender": {"login": "octocat"},
    }


def send(
    client, event: str, payload: Any, *, secret: str | None = SECRET, delivery: str = "d-1", raw: bytes | None = None
):
    """Post one delivery signed with `secret`, or unsigned when it is `None`."""
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}
    if secret is not None:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(WEBHOOK_PATH, content=body, headers=headers)


def test_an_unsigned_delivery_is_refused(client, queue):
    """No signature header is a 401 in the API's error envelope, and nothing is queued."""
    response = send(client, "push", push(), secret=None)
    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "GITHUB_WEBHOOK_REFUSED"
    assert queued(queue) == []


def test_a_delivery_signed_with_another_secret_is_refused(client, queue):
    """A signature under the wrong secret is a 401."""
    assert send(client, "push", push(), secret="not-the-secret").status_code == 401
    assert queued(queue) == []


def test_a_bad_signature_never_reaches_the_handler(client, queue, monkeypatch):
    """The gate answers before routing, so even a body the handler would reject is a 401."""
    monkeypatch.setattr(webhooks_router, "delivery_message", lambda *args: pytest.fail("handler ran"))
    assert send(client, "push", None, secret="wrong", raw=b"not json").status_code == 401


def test_with_no_secret_configured_every_delivery_is_refused(client, queue, monkeypatch):
    """An App without a webhook secret accepts nothing, however the delivery is signed."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    loader.invalidate()
    assert send(client, "push", push()).status_code == 401
    assert queued(queue) == []


def test_a_signed_push_is_queued_with_its_changed_paths(client, queue):
    """The message carries the repository, the commit, the branch and the union of changed paths."""
    response = send(client, "push", push())
    assert response.status_code == 202
    [message] = queued(queue)
    assert message["kind"] == WEBHOOK_KIND
    assert message["delivery"] == "d-1"
    assert message["repo"] == REPO
    assert message["repository_id"] == str(REPOSITORY_ID)
    assert message["installation_id"] == INSTALLATION_ID
    assert message["sha"] == SHA
    assert message["branch"] == "main"
    assert message["paths"] == ["infra/main.tf", "infra/new.tf", "old.tf"]
    assert message["received_at_ms"] > 0
    assert parse_message({"body": json.dumps(message)})["sha"] == SHA


def test_a_signed_pull_request_is_queued_against_its_merge_ref(client, queue):
    """A pull request message names the merge ref, the number and the unverified head."""
    assert send(client, "pull_request", pull_request()).status_code == 202
    [message] = queued(queue)
    assert message["ref"] == "refs/pull/7/merge"
    assert message["pr_number"] == 7
    assert message["head_sha"] == SHA
    assert message["base_branch"] == "main"


def test_a_ping_is_answered_and_not_queued(client, queue):
    """GitHub's ping on hook setup gets a 200."""
    assert send(client, "ping", {"zen": "Keep it logically awesome."}).status_code == 200
    assert queued(queue) == []


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("pull_request", pull_request(head_repository=999)),
        ("pull_request", pull_request(action="closed")),
        ("push", push(ref="refs/tags/v1.0.0")),
        ("push", push(deleted=True, after="0" * 40)),
        ("issues", {"action": "opened", "repository": {"id": REPOSITORY_ID, "full_name": REPO}}),
    ],
    ids=["fork", "closed", "tag", "branch-deleted", "other-event"],
)
def test_deliveries_the_bridge_ignores_are_acknowledged(client, queue, event, payload):
    """A fork's pull request, a closed one, a tag, a deleted branch and other events queue nothing."""
    assert send(client, event, payload).status_code == 202
    assert queued(queue) == []


def test_a_signed_body_that_is_not_json_is_a_400(client, queue):
    """A verified but unreadable body is refused by the handler."""
    assert send(client, "push", None, raw=b"not json").status_code == 400


def test_the_gate_leaves_other_routes_alone(auth_client, queue):
    """Only the webhook path is guarded; the rest of the github domain answers as before."""
    assert auth_client.get("/api/v1/github/app").status_code == 200
    assert auth_client.post("/api/v1/github/webhooks/other").status_code == 404


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"created": True}, None),
        ({"forced": True}, None),
        ({"before": "0" * 40}, None),
        ({"commits": []}, None),
    ],
    ids=["new-branch", "forced", "zero-before", "no-commits"],
)
def test_a_push_the_payload_cannot_diff_means_every_path(overrides, expected):
    """A new branch, a forced push and an empty commit list all mean every path."""
    message = delivery_message("push", "d-1", push(**overrides))
    assert message is not None
    assert message["paths"] is expected


def test_a_large_message_drops_its_paths(client, queue):
    """A message past the queue budget goes out saying every path."""
    commits = [{"added": [f"dir/{index:05d}-{'x' * 80}.tf"], "removed": [], "modified": []} for index in range(2500)]
    assert send(client, "push", push(commits=commits)).status_code == 202
    [message] = queued(queue)
    assert message["paths"] is None
