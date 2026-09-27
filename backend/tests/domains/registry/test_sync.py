"""Importing a repository's existing tags: on connect, on resync, and through the tag consumer."""

from __future__ import annotations

import json

import httpx
import pytest
from webbpulse.integrations.github import GitHubAppClient

from app.common.core.auth import REGISTRY_READ
from app.common.db import repositories
from app.common.github.archive import TagsUnavailable, list_tags
from app.common.github.loader import github_app_settings
from app.common.github.webhooks import TAG_KIND, MalformedDelivery, semver_version
from app.domains.registry import service
from app.domains.registry.consumers import dispatch, sync, tags
from tests.domains.registry.conftest import INSTALLATION_ID, OTHER, REPO, drain

BASE = "/api/v1/registry/modules"
ADDRESS = "WebbPulse/example/aws"
PK = service.module_pk("WebbPulse", "example", "aws")
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def _sync_record(module: str = PK, delivery: str = "sync-1") -> dict:
    """A queued sync request for one module."""
    return {"body": json.dumps({"kind": service.SYNC_KIND, "delivery": delivery, "module": module, "actor": "u"})}


def _row(settings, version: str) -> dict:
    """The stored version row of the example module."""
    return repositories.registry(settings).get({"pk": PK, "sk": service.version_sk(version)}) or {}


def _bodies(records: list[dict]) -> list[dict]:
    """The decoded bodies of queued records."""
    return [json.loads(record["body"]) for record in records]


@pytest.mark.parametrize(
    ("tag", "version"),
    [("v1.2.3", "1.2.3"), ("1.2.3", "1.2.3"), ("v1.0.0-rc.1", "1.0.0-rc.1"), ("release-1", None), ("v1.2", None)],
)
def test_only_semantic_version_tags_name_a_version(tag, version):
    """`vX.Y.Z` and `X.Y.Z`, prerelease allowed; anything else is ignored."""
    assert semver_version(tag) == version


def test_connecting_queues_a_tag_import(ingest_queue, auth_client, github):
    """The sync is queued on the registry ingest queue and names the new module."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO})

    assert response.status_code == 201, response.text
    bodies = _bodies(drain(ingest_queue))
    assert [(body["kind"], body["module"]) for body in bodies] == [(service.SYNC_KIND, PK)]
    assert bodies[0]["delivery"].startswith("sync-")


def test_connecting_can_leave_tags_for_a_resync(ingest_queue, auth_client, github):
    """`import_tags: false` connects without queueing anything."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO, "import_tags": False})

    assert response.status_code == 201, response.text
    assert drain(ingest_queue) == []


def test_connecting_without_a_queue_still_connects(auth_client, github):
    """A sync that cannot be queued does not undo the connection; a resync recovers it."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO})

    assert response.status_code == 201, response.text


def test_a_sync_queues_each_unsettled_semver_tag_for_that_module_only(ingest_queue, settings, auth_client, github):
    """Non-semver tags are ignored, `v` wins over a bare duplicate, and every message is scoped."""
    auth_client.post(BASE, json={"vcs_repo": REPO})
    drain(ingest_queue)
    github.tags = [("v1.1.0", SHA_A), ("1.1.0", SHA_B), ("1.0.0", SHA_C), ("latest", SHA_A), ("v2", SHA_A)]

    assert sync.handle_record(_sync_record(), settings=settings) == {"1.1.0": "v1.1.0", "1.0.0": "1.0.0"}

    bodies = _bodies(drain(ingest_queue))
    assert sorted((body["version"], body["tag"], body["sha"]) for body in bodies) == [
        ("1.0.0", "1.0.0", SHA_C),
        ("1.1.0", "v1.1.0", SHA_A),
    ]
    assert {body["kind"] for body in bodies} == {TAG_KIND}
    assert {body["module"] for body in bodies} == {PK}
    assert {body["installation_id"] for body in bodies} == {str(INSTALLATION_ID)}


def test_the_queued_tags_publish_through_the_tag_consumer(ingest_queue, settings, auth_client, github):
    """A synced tag publishes exactly as a pushed one, and only for the module that synced."""
    auth_client.post(BASE, json={"vcs_repo": REPO})
    auth_client.post(BASE, json={"vcs_repo": REPO, "name": "other", "provider": "aws", "import_tags": False})
    drain(ingest_queue)
    github.tags = [("v1.0.0", SHA_A)]
    sync.handle_record(_sync_record(), settings=settings)

    outcomes = [dispatch.route_record(record, settings=settings) for record in drain(ingest_queue)]

    assert outcomes == [None]
    assert _row(settings, "1.0.0")["status"] == "published"
    assert _row(settings, "1.0.0")["tag"] == "v1.0.0"
    other = service.get_module("WebbPulse", "other", "aws", settings=settings)
    assert other["versions"] == []


def test_a_sync_skips_what_is_settled_and_retries_what_changed(ingest_queue, settings, auth_client, github):
    """Published stays published, a failure at the same commit is not refetched, a moved tag is retried."""
    auth_client.post(BASE, json={"vcs_repo": REPO, "import_tags": False})
    github.tags = [("v1.0.0", SHA_A)]
    tags.handle_record(_as_tag("1.0.0", SHA_A), settings=settings)
    table = repositories.registry(settings)
    for version, status, sha in (("1.1.0", "failed", SHA_B), ("1.2.0", "failed", SHA_B)):
        table.put({"pk": PK, "sk": service.version_sk(version), "version": version, "status": status, "sha": sha})
    github.tags = [("v1.0.0", SHA_A), ("v1.1.0", SHA_B), ("v1.2.0", SHA_C)]

    assert sync.handle_record(_sync_record(), settings=settings) == {"1.2.0": "v1.2.0"}
    assert [body["version"] for body in _bodies(drain(ingest_queue))] == ["1.2.0"]


def _as_tag(version: str, sha: str) -> dict:
    """A scoped tag message for the example module, as a sync queues it."""
    body = {
        "kind": TAG_KIND,
        "delivery": "sync-0",
        "event": "sync",
        "repo": REPO,
        "repository_id": "515151",
        "installation_id": str(INSTALLATION_ID),
        "actor": "u",
        "ref": f"refs/tags/v{version}",
        "tag": f"v{version}",
        "version": version,
        "sha": sha,
        "module": PK,
    }
    return {"body": json.dumps(body)}


def test_a_scoped_tag_leaves_other_modules_on_the_repository_alone(settings, auth_client, github):
    """Only the module the message names publishes; a webhook tag still reaches every module."""
    auth_client.post(BASE, json={"vcs_repo": REPO})
    auth_client.post(BASE, json={"vcs_repo": REPO, "name": "other", "provider": "aws"})

    assert tags.handle_record(_as_tag("1.0.0", SHA_A), settings=settings) == {ADDRESS: "published"}


def test_paging_and_the_version_cap_bound_a_sync(ingest_queue, settings, auth_client, github, monkeypatch):
    """No more than `MAX_TAG_PAGES` pages are read and the newest `MAX_SYNC_VERSIONS` queued."""
    monkeypatch.setattr(sync, "MAX_TAG_PAGES", 2)
    monkeypatch.setattr(sync, "MAX_SYNC_VERSIONS", 3)
    auth_client.post(BASE, json={"vcs_repo": REPO, "import_tags": False})
    github.tags = [(f"v0.0.{index}", SHA_A) for index in range(250)]

    queued = sync.handle_record(_sync_record(), settings=settings)

    assert github.tag_pages() == 2
    assert sorted(queued, key=service.version_order) == ["0.0.197", "0.0.198", "0.0.199"]
    assert len(drain(ingest_queue)) == 3


def test_a_deleted_module_syncs_nothing(ingest_queue, settings, github):
    """A sync that outlived its module lists no tags."""
    assert sync.handle_record(_sync_record(), settings=settings) == {}
    assert github.tag_pages() == 0


def test_a_github_failure_listing_tags_raises_for_a_retry(ingest_queue, settings, auth_client, github):
    """SQS retries a sync GitHub refused."""
    auth_client.post(BASE, json={"vcs_repo": REPO, "import_tags": False})
    github.tags_status = 502

    with pytest.raises(TagsUnavailable):
        sync.handle_record(_sync_record(), settings=settings)


def test_a_malformed_sync_is_parked():
    """A sync with no module cannot be acted on."""
    with pytest.raises(MalformedDelivery):
        sync.handle_record({"body": json.dumps({"kind": service.SYNC_KIND, "delivery": "x"})})


def test_list_tags_stops_at_a_short_page(settings, github):
    """A page shorter than the page size is the last one."""
    github.tags = [(f"v1.0.{index}", SHA_A) for index in range(150)]
    credentials = github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    with (
        httpx.Client(transport=httpx.MockTransport(github.handle)) as http,
        GitHubAppClient.from_settings(credentials, client=http) as app,
    ):
        listed = list_tags(app, http, installation_id=INSTALLATION_ID, repository=REPO, max_pages=10)

    assert len(listed) == 150
    assert github.tag_pages() == 2


def test_resync_queues_a_sync(ingest_queue, auth_client, github):
    """The resync route answers 202 with the sync's delivery id."""
    auth_client.post(BASE, json={"vcs_repo": REPO, "import_tags": False})

    response = auth_client.post(f"{BASE}/WebbPulse/example/aws/resync")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["source"] == ADDRESS
    assert [queued["delivery"] for queued in _bodies(drain(ingest_queue))] == [body["delivery"]]


def test_resync_of_an_unknown_module_is_a_404(ingest_queue, auth_client, github):
    """Nothing to sync."""
    response = auth_client.post(f"{BASE}/WebbPulse/missing/aws/resync")

    assert response.status_code == 404
    assert response.json()["error_code"] == "REGISTRY_NOT_FOUND"


def test_resync_without_a_queue_is_a_503(auth_client, github):
    """A sync that cannot be queued is reported, not swallowed."""
    auth_client.post(BASE, json={"vcs_repo": OTHER["full_name"], "name": "network", "provider": "aws"})

    response = auth_client.post(f"{BASE}/WebbPulse/network/aws/resync")

    assert response.status_code == 503
    assert response.json()["error_code"] == "REGISTRY_SYNC_UNAVAILABLE"


def test_resync_needs_registry_write(scoped_client, module):
    """Reading the registry is not enough to trigger a sync."""
    with scoped_client(REGISTRY_READ) as client:
        assert client.post(f"{BASE}/WebbPulse/example/aws/resync").status_code == 403
