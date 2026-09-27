"""The webhooks consumer: a verified delivery becoming the same upload a workflow made.

GitHub is replaced at the HTTP layer, the tarball included, so the real App client
mints a real App JWT and an installation token, and the archive arrives through
the same redirect codeload answers with. The ingest record and object land in moto,
and the existing ingest consumer is then driven on that object to prove the handoff.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from vcs_helpers import BASE_SHA, HEAD_SHA, MERGE_SHA, REPO, REPOSITORY_ID

from app.common.composition import settings as settings_module
from app.common.db import repositories
from app.common.github import loader
from app.common.github.webhooks import WEBHOOK_KIND, MalformedDelivery
from app.domains.runs import reporting, vcs
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import dispatch, ingest, webhooks

APP_ID = 5150
INSTALLATION_ID = 777
ROLE = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run"
CODELOAD = "https://codeload.github.com/WebbPulse/example-infra/legacy.tar.gz/sha"


def github_tarball(prefix: str = "WebbPulse-example-infra-aaaaaaa") -> bytes:
    """An archive shaped like GitHub's: one top level directory, a link and a stray changed paths file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        top = tarfile.TarInfo(prefix)
        top.type = tarfile.DIRTYPE
        archive.addfile(top)
        for name, content in {
            "main.tf": b'resource "null_resource" "x" {}\n',
            "infra/main.tf": b"# infra\n",
            ".webbpulse/changed-paths.txt": b"forged\n",
            "terraform.tfstate": b"{}",
        }.items():
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo(f"{prefix}/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    return buffer.getvalue()


@dataclass
class FakeGitHub:
    """The reads the consumer makes, answered from plain values."""

    pulls: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    archive: bytes = field(default_factory=github_tarball)
    redirect: str = CODELOAD
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as GitHub would."""
        self.requests.append(request)
        path = request.url.path
        base = f"/repos/{REPO}"
        if request.url.host == "codeload.github.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.archive)
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"{base}/installation":
            return httpx.Response(200, json={"id": INSTALLATION_ID})
        if re.fullmatch(rf"{base}/tarball/\w+", path):
            return httpx.Response(302, headers={"Location": self.redirect})
        if path == f"{base}/pulls/7/files":
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.files[(page - 1) * 100 : page * 100])
        if path == f"{base}/pulls/7":
            answer = self.pulls.pop(0) if len(self.pulls) > 1 else self.pulls[0]
            return httpx.Response(200, json=answer)
        return httpx.Response(404, json={"message": "Not Found"})

    def tarball_fetches(self) -> int:
        """How many times the archive was requested from the API."""
        return sum(1 for request in self.requests if "/tarball/" in request.url.path)


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    """One RSA key for the whole session."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def bind(auth_client):
    """A factory creating a workspace bound to the test repository, before the App exists."""

    def create(name: str = "infra", **fields: Any) -> dict[str, Any]:
        payload = {
            "name": name,
            "engine_version": "1.11.4",
            "run_role_arn": ROLE,
            "vcs_repo": REPO,
            "tracked_branch": "main",
        } | fields
        response = auth_client.post("/api/v1/workspaces", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> Iterator[Any]:
    """A factory that configures the App against a fake GitHub, called after binding."""

    def enable() -> FakeGitHub:
        fake = FakeGitHub()
        monkeypatch.setattr(reporting, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handle)))
        monkeypatch.setattr(webhooks, "sleep", lambda seconds: None)
        monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", private_key_pem)
        settings_module.reset_settings_cache()
        loader.invalidate()
        return fake

    yield enable
    loader.invalidate()
    settings_module.reset_settings_cache()


@pytest.fixture(autouse=True)
def reported(monkeypatch):
    """The uploads reported as starting no run, as upload id and skipped workspaces."""
    calls: list[tuple[str, list[dict]]] = []

    def record(upload, skipped, *, settings=None):
        calls.append((str(upload["upload_id"]), [dict(item) for item in skipped]))
        return True

    monkeypatch.setattr(reporting, "report_upload", record)
    return calls


def push_message(delivery: str = "d-push", **overrides: Any) -> dict[str, Any]:
    """A queued push to main."""
    body = {
        "kind": WEBHOOK_KIND,
        "delivery": delivery,
        "event": "push",
        "repo": REPO,
        "repository_id": REPOSITORY_ID,
        "installation_id": INSTALLATION_ID,
        "actor": "octocat",
        "ref": "refs/heads/main",
        "sha": HEAD_SHA,
        "branch": "main",
        "paths": ["main.tf"],
        "received_at_ms": 1_790_000_000_000,
    }
    return body | overrides


def pr_message(delivery: str = "d-pr") -> dict[str, Any]:
    """A queued pull request 7 from a branch of the same repository."""
    return {
        "kind": WEBHOOK_KIND,
        "delivery": delivery,
        "event": "pull_request",
        "repo": REPO,
        "repository_id": REPOSITORY_ID,
        "installation_id": INSTALLATION_ID,
        "actor": "octocat",
        "ref": "refs/pull/7/merge",
        "pr_number": 7,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "base_branch": "main",
        "received_at_ms": 1_790_000_000_000,
    }


def consume(message: dict[str, Any], settings) -> str | None:
    """Hand one message to the consumer."""
    return webhooks.handle_record({"body": json.dumps(message)}, settings=settings)


def consumed(message: dict[str, Any], settings) -> str:
    """Hand one message to the consumer and require the upload id it wrote."""
    upload_id = consume(message, settings)
    assert upload_id is not None
    return upload_id


def members(settings, upload_id: str) -> dict[str, bytes]:
    """The files of the ingest object, by name."""
    body = boto3.client("s3", region_name="us-west-2").get_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=vcs.ingest_key(upload_id)
    )["Body"]
    found: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(body.read()), mode="r:gz") as archive:
        for member in archive.getmembers():
            handle = archive.extractfile(member) if member.isfile() else None
            found[member.name] = handle.read() if handle else b""
    return found


def ingested(settings, upload_id: str) -> list[str]:
    """Drive the ingest consumer on the object this upload wrote."""
    upload = repositories.vcs_uploads(settings).get({"upload_id": upload_id}) or {}
    body = {
        "kind": ingest.INGEST_KIND,
        "bucket": settings.ARTIFACTS_BUCKET,
        "key": vcs.ingest_key(upload_id),
        "size": int(upload["size_bytes"]),
    }
    return ingest.handle_record({"body": json.dumps(body)}, settings=settings)


def test_a_push_is_fetched_repacked_and_ingested(settings, bind, github, state_machine):
    """The archive loses GitHub's top directory, links and state, gains the changed paths, and starts a run."""
    workspace = bind()
    fake = github()
    upload_id = consumed(push_message(), settings)
    assert upload_id == webhooks.upload_id_for("d-push")
    files = members(settings, upload_id)
    assert files[ingest.CHANGED_PATHS_MEMBER] == b"main.tf\n"
    assert "main.tf" in files and "infra/main.tf" in files
    assert "escape" not in files and "terraform.tfstate" not in files
    assert not any(name.startswith("WebbPulse-") for name in files)
    record = repositories.vcs_uploads(settings).get({"upload_id": upload_id}) or {}
    assert record["sha"] == HEAD_SHA and record["branch"] == "main" and record["source"] == "webhook"
    [run_id] = ingested(settings, upload_id)
    run = runs_service.get_run(run_id, settings=settings)
    assert run["workspace_id"] == workspace["workspace_id"]
    assert run["source"] == "vcs_push"
    assert fake.tarball_fetches() == 1


def test_a_redelivered_message_writes_nothing_twice(settings, bind, github, state_machine):
    """The same delivery maps to the same upload, and a written one is not fetched again."""
    bind()
    fake = github()
    first = consumed(push_message(), settings)
    assert consume(push_message(), settings) == first
    assert fake.tarball_fetches() == 1


def test_a_push_to_an_untracked_branch_posts_no_check(settings, bind, github, reported):
    """A branch no workspace tracks gets no aggregate, no record and no fetch."""
    bind()
    fake = github()
    message = push_message(branch="feature", ref="refs/heads/feature")
    assert consume(message, settings) is None
    assert reported == []
    assert fake.tarball_fetches() == 0
    assert repositories.vcs_uploads(settings).get({"upload_id": webhooks.upload_id_for("d-push")}) is None


def test_a_push_to_a_tracked_branch_touching_no_watched_path_is_reported(settings, bind, github, reported):
    """The tracked branch still gets "No runs needed" when the push starts no run."""
    bind(working_directory="infra")
    fake = github()
    upload_id = consumed(push_message(paths=["README.md"]), settings)
    assert reported == [(upload_id, [])]
    assert fake.tarball_fetches() == 0
    assert consume(push_message(paths=["README.md"]), settings) == upload_id
    assert len(reported) == 1


def test_an_unbound_repository_push_posts_no_check(settings, github, reported):
    """With no bound workspace no branch is tracked, so a push posts nothing."""
    github()
    assert consume(push_message(), settings) is None
    assert reported == []


def test_an_unbound_repository_pull_request_is_still_reported(settings, github, reported):
    """A pull request always gets the aggregate, bound or not."""
    fake = github()
    fake.pulls = [{"state": "open", "head": {"sha": HEAD_SHA}, "mergeable": True, "merge_commit_sha": MERGE_SHA}]
    fake.files = [{"filename": "README.md"}]
    upload_id = consumed(pr_message(), settings)
    assert reported == [(upload_id, [])]


def test_paths_outside_the_trigger_patterns_start_nothing(settings, bind, github, reported):
    """A push touching only other paths is reported and not fetched."""
    bind(working_directory="infra")
    fake = github()
    consume(push_message(paths=["docs/readme.md"]), settings)
    assert fake.tarball_fetches() == 0
    assert len(reported) == 1


def test_a_pull_request_waits_for_its_merge_commit(settings, bind, github, state_machine):
    """Mergeable unknown is read again; the plan uses the merge commit and reports on the head."""
    bind()
    fake = github()
    fake.pulls = [
        {"state": "open", "head": {"sha": HEAD_SHA}, "mergeable": None, "merge_commit_sha": None},
        {"state": "open", "head": {"sha": HEAD_SHA}, "mergeable": True, "merge_commit_sha": MERGE_SHA},
    ]
    fake.files = [{"filename": "main.tf"}, {"filename": "new.tf", "previous_filename": "old.tf"}]
    upload_id = consumed(pr_message(), settings)
    record = repositories.vcs_uploads(settings).get({"upload_id": upload_id}) or {}
    assert record["sha"] == MERGE_SHA
    assert record["head_sha"] == HEAD_SHA
    assert record["pr_number"] == 7
    assert members(settings, upload_id)[ingest.CHANGED_PATHS_MEMBER] == b"main.tf\nnew.tf\nold.tf\n"
    assert any(f"/tarball/{MERGE_SHA}" in request.url.path for request in fake.requests)
    [run_id] = ingested(settings, upload_id)
    assert runs_service.get_run(run_id, settings=settings)["source"] == "vcs_pr"


def test_a_pull_request_still_computing_is_retried(settings, bind, github):
    """A merge state that never settles raises, so SQS delivers the message again."""
    bind()
    fake = github()
    fake.pulls = [{"state": "open", "head": {"sha": HEAD_SHA}, "mergeable": None, "merge_commit_sha": None}]
    with pytest.raises(webhooks.MergeStatePending):
        consume(pr_message(), settings)


@pytest.mark.parametrize(
    "pull",
    [
        {"state": "open", "head": {"sha": "f" * 40}, "mergeable": True, "merge_commit_sha": MERGE_SHA},
        {"state": "open", "head": {"sha": HEAD_SHA}, "mergeable": False, "merge_commit_sha": None},
        {"state": "closed", "head": {"sha": HEAD_SHA}, "mergeable": True, "merge_commit_sha": MERGE_SHA},
    ],
    ids=["head-moved", "conflicts", "closed"],
)
def test_a_pull_request_github_has_moved_past_is_dropped(settings, bind, github, reported, pull):
    """A newer head, a conflict or a closed request leave nothing behind."""
    bind()
    fake = github()
    fake.pulls = [pull]
    assert consume(pr_message(), settings) is None
    assert reported == []
    assert fake.tarball_fetches() == 0


def test_a_capped_file_listing_means_every_path(settings, bind, github, state_machine):
    """Past the listing cap the changed paths are `*`."""
    bind()
    fake = github()
    fake.pulls = [{"state": "open", "head": {"sha": HEAD_SHA}, "mergeable": True, "merge_commit_sha": MERGE_SHA}]
    fake.files = [{"filename": f"f{index}.tf"} for index in range(500)]
    upload_id = consumed(pr_message(), settings)
    assert members(settings, upload_id)[ingest.CHANGED_PATHS_MEMBER] == b"*\n"


def test_a_redirect_off_codeload_is_refused(settings, bind, github):
    """The archive is only followed to GitHub's own download host."""
    bind()
    fake = github()
    fake.redirect = "https://attacker.example.com/archive.tar.gz"
    with pytest.raises(webhooks.TarballUnavailable):
        consume(push_message(), settings)


def test_the_events_route_hands_the_kind_to_this_consumer(settings, bind, github, state_machine):
    """The dispatcher knows the kind, so the runs function consumes the queue."""
    bind()
    github()
    dispatch.route_record({"body": json.dumps(push_message())}, settings=settings)
    assert repositories.vcs_uploads(settings).get({"upload_id": webhooks.upload_id_for("d-push")})


def test_a_malformed_message_is_refused(settings):
    """A body lacking its event's fields parks on the dead letter queue."""
    with pytest.raises(MalformedDelivery):
        consume({"kind": WEBHOOK_KIND, "event": "push"}, settings)
