"""Fixtures for the registry domain: a fake GitHub verifier, an allowlist and tarballs.

GitHub's signing keys cannot be reached from the suite, so the verifier is swapped
for one that accepts a token naming a known claim set and rejects anything else
with the same `InvalidToken` the real one raises.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any, Callable

import boto3
import pytest
from webbpulse.identity.service import InvalidToken

from app.common.composition import settings as settings_module
from app.common.github import oidc

REPO = "WebbPulse/terraform-aws-example"
OVERRIDE_REPO = "WebbPulse/webbpulse-terraform-staging-e2e"
REPOSITORY_ID = "515151"
SHA = "d" * 40


def claims(**overrides: Any) -> dict[str, Any]:
    """A verified tag push token's claims, with `overrides` applied."""
    base = {
        "iss": oidc.GITHUB_ISSUER,
        "aud": "webbpulse-terraform",
        "sub": f"repo:{REPO}:ref:refs/tags/v1.2.3",
        "repository": REPO,
        "repository_id": REPOSITORY_ID,
        "repository_owner": "WebbPulse",
        "event_name": "push",
        "ref": "refs/tags/v1.2.3",
        "sha": SHA,
        "actor": "octocat",
        "run_id": "7000",
        "run_attempt": "1",
    }
    return base | overrides


def token_for(values: dict[str, Any]) -> str:
    """The bearer the fake verifier accepts for `values`."""
    return "fake." + json.dumps(values, sort_keys=True)


class FakeVerifier:
    """Accepts `token_for` tokens and rejects every other string."""

    def verify(self, token: str, *, expected_type: str | None = None) -> dict[str, Any]:
        """The claims the token names, or `InvalidToken`."""
        if not token.startswith("fake."):
            raise InvalidToken("signature verification failed")
        return json.loads(token.removeprefix("fake."))


def tarball(files: dict[str, str] | None = None, *, extra: Callable[[tarfile.TarFile], None] | None = None) -> bytes:
    """A gzipped module tarball rooted at `./`."""
    contents = files if files is not None else {"main.tf": 'variable "x" {}\n'}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in sorted(contents):
            data = contents[name].encode()
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if extra is not None:
            extra(archive)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def allowlist(monkeypatch):
    """Two allowlisted repositories: one named by convention, one by override."""
    monkeypatch.setenv(
        "REGISTRY_REPOSITORIES",
        json.dumps({REPO: "", OVERRIDE_REPO: "registry-proof/null"}),
    )
    settings_module.reset_settings_cache()
    yield
    settings_module.reset_settings_cache()


@pytest.fixture(autouse=True)
def verifier(monkeypatch):
    """The fake GitHub verifier the upload route resolves for any audience."""
    fake = FakeVerifier()
    monkeypatch.setattr(oidc, "verifier", lambda audience: fake)
    return fake


@pytest.fixture
def upload(client):
    """A factory POSTing an upload for `values` and returning the response."""

    def post(values: dict[str, Any], size_bytes: int = 1024):
        return client.post(
            "/api/v1/registry/uploads",
            json={"size_bytes": size_bytes},
            headers={"Authorization": f"Bearer {token_for(values)}"},
        )

    return post


@pytest.fixture
def ingest(settings):
    """A factory putting a tarball at an upload's key and running the consumer on it."""
    from app.domains.registry.consumers import ingest as consumer
    from app.domains.registry.service import incoming_key

    def run(upload_id: str, data: bytes) -> str:
        key = incoming_key(upload_id)
        boto3.client("s3", region_name="us-west-2").put_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key, Body=data)
        body = {"kind": consumer.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": key, "size": len(data)}
        return consumer.handle_record({"body": json.dumps(body)}, settings=settings)

    return run
