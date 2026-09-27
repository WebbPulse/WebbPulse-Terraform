"""Fixtures for the registry domain: a configured App talking to a fake GitHub.

GitHub is replaced at the HTTP layer with `httpx.MockTransport`, so the real client
signs a real App JWT, finds the installation, lists what it sees and follows the
tarball redirect to codeload the way it does in production.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.common.composition import settings as settings_module
from app.common.github import loader
from app.common.github.webhooks import TAG_KIND
from app.domains.registry import service
from app.domains.registry.consumers import tags

REPO = "WebbPulse/terraform-aws-example"
REPOSITORY_ID = 515151
INSTALLATION_ID = 7001
SHA = "d" * 40
CODELOAD = "https://codeload.github.com/WebbPulse/terraform-aws-example/legacy.tar.gz/sha"
REPOSITORY = {"id": REPOSITORY_ID, "full_name": REPO, "default_branch": "main"}
OTHER = {"id": 515152, "full_name": "WebbPulse/modules", "default_branch": "main"}


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    """One RSA key for the session, since generating it is the slow part."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def github_tarball(
    files: dict[str, str] | None = None,
    *,
    prefix: str = "WebbPulse-terraform-aws-example-ddddddd",
    extra: Callable[[tarfile.TarFile], None] | None = None,
) -> bytes:
    """A repository archive shaped like GitHub's, everything under one top directory."""
    contents = files if files is not None else {"main.tf": 'variable "x" {}\n', "modules/child/main.tf": ""}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        top = tarfile.TarInfo(f"{prefix}/")
        top.type = tarfile.DIRTYPE
        archive.addfile(top)
        for name in sorted(contents):
            data = contents[name].encode()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if extra is not None:
            extra(archive)
    return buffer.getvalue()


@dataclass
class FakeGitHub:
    """The reads module connection and tag publishing make, answered from plain values."""

    repositories: list[dict[str, Any]] = field(default_factory=lambda: [REPOSITORY, OTHER])
    archive: bytes = field(default_factory=github_tarball)
    redirect: str = CODELOAD
    requests: list[httpx.Request] = field(default_factory=list)
    failure: int | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as GitHub would."""
        self.requests.append(request)
        if self.failure is not None:
            return httpx.Response(self.failure, json={"message": "failed"})
        path = request.url.path
        if request.url.host == "codeload.github.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.archive)
        known = {str(item["full_name"]).lower() for item in self.repositories}
        if match := re.fullmatch(r"/repos/([^/]+/[^/]+)/installation", path):
            if match.group(1).lower() not in known:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": INSTALLATION_ID})
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            return httpx.Response(200, json={"total_count": len(self.repositories), "repositories": self.repositories})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/tarball/\w+", path):
            return httpx.Response(302, headers={"Location": self.redirect})
        return httpx.Response(404, json={"message": "Not Found"})

    def tarball_fetches(self) -> int:
        """How many times an archive was requested from the API."""
        return sum(1 for request in self.requests if "/tarball/" in request.url.path)


@pytest.fixture(autouse=True)
def no_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No App until a test configures one, and an empty credentials cache around each test."""
    for key in ("GITHUB_APP_ID", "GITHUB_PRIVATE_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings_module.reset_settings_cache()
    loader.invalidate()
    yield
    loader.invalidate()
    settings_module.reset_settings_cache()


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> FakeGitHub:
    """A configured App talking to a fake GitHub, for connecting and for publishing."""
    fake = FakeGitHub()
    monkeypatch.setenv("GITHUB_APP_ID", "424242")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", private_key_pem)
    settings_module.reset_settings_cache()
    loader.invalidate()

    def client() -> httpx.Client:
        """A client whose every request reaches the fake."""
        return httpx.Client(transport=httpx.MockTransport(fake.handle), follow_redirects=False)

    monkeypatch.setattr(service, "http_client", client)
    monkeypatch.setattr(tags, "http_client", client)
    return fake


def tag_record(version: str = "1.2.3", *, sha: str = SHA, delivery: str = "d-1", prefix: str = "v") -> dict[str, Any]:
    """The SQS record the webhook route queues for a tag push."""
    body = {
        "kind": TAG_KIND,
        "delivery": delivery,
        "event": "push",
        "repo": REPO,
        "repository_id": str(REPOSITORY_ID),
        "installation_id": INSTALLATION_ID,
        "actor": "octocat",
        "ref": f"refs/tags/{prefix}{version}",
        "tag": f"{prefix}{version}",
        "version": version,
        "sha": sha,
        "received_at_ms": 0,
    }
    return {"body": json.dumps(body)}


@pytest.fixture
def module(auth_client, github) -> dict[str, Any]:
    """The example module, connected to `REPO` by convention."""
    response = auth_client.post("/api/v1/registry/modules", json={"vcs_repo": REPO})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def publish(settings) -> Callable[..., dict[str, str]]:
    """A factory running the tag consumer on one tag push."""

    def run(version: str = "1.2.3", **kwargs: Any) -> dict[str, str]:
        """Each connected module's outcome for `version`."""
        return tags.handle_record(tag_record(version, **kwargs), settings=settings)

    return run
