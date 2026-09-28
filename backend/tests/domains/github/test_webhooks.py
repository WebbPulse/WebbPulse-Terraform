"""The webhook route: the signature gate in front of it and the message it queues.

Deliveries are signed with a known secret the way GitHub signs them, and the queue
is moto's, so what is under test is what a real delivery produces: a refusal before
any handler runs, an acknowledgement, or exactly one compact message.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import pytest

from app.common.composition import settings as settings_module
from app.common.github import loader
from app.common.github.webhooks import (
    MAX_DELIVERY_AGE,
    TAG_KIND,
    WEBHOOK_KIND,
    WEBHOOK_PATH,
    MalformedDelivery,
    delivery_message,
    is_stale,
    parse_message,
    parse_tag_message,
    tag_message,
)
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


@pytest.fixture
def registry_queue(monkeypatch: pytest.MonkeyPatch, queue: str) -> Iterator[str]:
    """The registry's ingest queue, wired into the settings alongside the webhooks queue."""
    url = boto3.client("sqs", region_name="us-west-2").create_queue(QueueName="registry-ingest")["QueueUrl"]
    monkeypatch.setenv("REGISTRY_INGEST_QUEUE_URL", url)
    settings_module.reset_settings_cache()
    yield url
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
        "repository": {"id": REPOSITORY_ID, "full_name": REPO, "pushed_at": int(time.time())},
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
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        ("push", push(ref="refs/tags/release-1")),
        ("push", push(deleted=True, after="0" * 40)),
        ("issues", {"action": "opened", "repository": {"id": REPOSITORY_ID, "full_name": REPO}}),
    ],
    ids=["fork", "closed", "tag", "non-semver-tag", "branch-deleted", "other-event"],
)
def test_deliveries_the_bridge_ignores_are_acknowledged(client, queue, event, payload):
    """A fork's pull request, a closed one, a tag, a deleted branch and other events queue nothing for runs."""
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


def test_a_semver_tag_push_goes_to_the_registry_queue(client, queue, registry_queue):
    """A version tag is queued for the registry and never for the runs function."""
    response = send(client, "push", push(ref="refs/tags/v1.2.3", head_commit={"id": SHA}))
    assert response.status_code == 202
    assert queued(queue) == []
    [message] = queued(registry_queue)
    assert message["kind"] == TAG_KIND
    assert message["tag"] == "v1.2.3"
    assert message["version"] == "1.2.3"
    assert message["sha"] == SHA
    assert message["repository_id"] == str(REPOSITORY_ID)
    assert message["installation_id"] == INSTALLATION_ID
    assert parse_tag_message({"body": json.dumps(message)})["version"] == "1.2.3"


@pytest.mark.parametrize(
    ("ref", "version"),
    [("refs/tags/1.0.0", "1.0.0"), ("refs/tags/v2.0.0-rc.1", "2.0.0-rc.1"), ("refs/tags/v0.10.3", "0.10.3")],
)
def test_version_tags_with_and_without_the_v_publish(ref, version):
    """`vX.Y.Z` and `X.Y.Z` both name a version, prereleases included."""
    message = tag_message("push", "d-1", push(ref=ref))
    assert message is not None
    assert message["version"] == version


@pytest.mark.parametrize(
    "ref",
    ["refs/tags/release-1", "refs/tags/v1.0", "refs/tags/v01.0.0", "refs/tags/latest", "refs/heads/v1.0.0"],
)
def test_other_refs_are_not_versions(ref):
    """Anything but a semantic version tag publishes nothing."""
    assert tag_message("push", "d-1", push(ref=ref)) is None


def test_a_deleted_tag_publishes_and_unpublishes_nothing(client, queue, registry_queue):
    """Deleting a tag is acknowledged and queues nothing anywhere."""
    payload = push(ref="refs/tags/v1.2.3", deleted=True, after="0" * 40)
    assert send(client, "push", payload).status_code == 202
    assert queued(queue) == []
    assert queued(registry_queue) == []


def test_an_annotated_tag_publishes_its_commit(client, queue, registry_queue):
    """For an annotated tag `after` is the tag object, so the head commit names the commit."""
    tag_object = "e" * 40
    assert (
        send(client, "push", push(ref="refs/tags/v1.2.3", after=tag_object, head_commit={"id": SHA})).status_code == 202
    )
    [message] = queued(registry_queue)
    assert message["sha"] == SHA


def test_a_tag_with_no_registry_here_is_acknowledged(client, queue):
    """An environment without the registry queue drops the tag rather than failing the delivery."""
    assert send(client, "push", push(ref="refs/tags/v1.2.3")).status_code == 202
    assert queued(queue) == []


def test_a_tag_message_missing_a_field_is_malformed():
    """A message the route did not build parks on the dead letter queue."""
    with pytest.raises(MalformedDelivery):
        parse_tag_message({"body": json.dumps({"kind": TAG_KIND, "repo": REPO})})
    with pytest.raises(MalformedDelivery):
        parse_tag_message({"body": json.dumps({"kind": WEBHOOK_KIND})})


def aged(payload: dict[str, Any], age: timedelta) -> dict[str, Any]:
    """A push payload whose GitHub push time is `age` ago."""
    repository = {**payload["repository"], "pushed_at": int(time.time() - age.total_seconds())}
    return {**payload, "repository": repository}


def test_a_push_older_than_the_redelivery_window_is_dropped(client, queue):
    """A captured push replayed after its dedupe record could expire is acknowledged and never queued."""
    payload = aged(push(), MAX_DELIVERY_AGE + timedelta(minutes=1))
    assert send(client, "push", payload, delivery="replayed").status_code == 202
    assert queued(queue) == []


def test_a_redelivery_inside_the_window_is_queued(client, queue):
    """GitHub's own redelivery of a two day old push still reaches the consumer, whose dedupe settles it."""
    payload = aged(push(), timedelta(days=2))
    assert send(client, "push", payload, delivery="redelivered").status_code == 202
    assert [message["delivery"] for message in queued(queue)] == ["redelivered"]


def test_a_push_with_no_event_time_is_dropped(client, queue):
    """A body that gives no time cannot be placed inside the window, so it is refused."""
    payload = push()
    payload["repository"] = {"id": REPOSITORY_ID, "full_name": REPO}
    assert send(client, "push", payload).status_code == 202
    assert queued(queue) == []


def test_a_stale_tag_push_never_reaches_the_registry(client, queue, registry_queue):
    """The window covers the registry's tag pushes too."""
    payload = aged(push(ref="refs/tags/v1.2.3"), timedelta(days=30))
    assert send(client, "push", payload).status_code == 202
    assert queued(registry_queue) == []


def test_a_stale_pull_request_is_dropped(client, queue):
    """A pull request carries its time in `updated_at`."""
    payload = pull_request()
    payload["pull_request"]["updated_at"] = "2020-01-01T00:00:00Z"
    assert send(client, "pull_request", payload).status_code == 202
    assert queued(queue) == []


def test_a_release_is_placed_by_its_published_time():
    """A release is judged by `published_at`, and a missing or unreadable time is stale."""
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    fresh = {"release": {"published_at": "2026-09-27T12:00:00Z"}}
    old = {"release": {"published_at": "2026-09-20T12:00:00Z"}}
    assert not is_stale("release", fresh, now)
    assert is_stale("release", old, now)
    assert is_stale("release", {"release": {"published_at": "yesterday"}}, now)
    assert is_stale("release", {}, now)


def test_the_dedupe_record_outlives_the_window():
    """A replay inside the window must find its record, so the record lasts longer than the window."""
    from app.domains.runs.vcs import RECORD_TTL

    assert RECORD_TTL > MAX_DELIVERY_AGE
