"""The provider registry: connecting, publishing from a signed release, and the protocol Terraform speaks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest

from app.common.core.auth import REGISTRY_READ, WORKSPACES_READ
from app.common.db import repositories
from app.common.github.webhooks import RELEASE_KIND, MalformedDelivery, parse_release_message, release_message
from app.domains.registry import providers, service
from app.domains.registry.consumers import dispatch, tags
from app.domains.registry.consumers import providers as consumer
from app.domains.registry.openpgp import SignatureInvalid, verify_detached
from tests.domains.registry.conftest import INSTALLATION_ID, drain
from tests.domains.registry.pgp import SigningKey

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "WebbPulse/terraform-provider-webbpulse"
REPOSITORY_ID = 616161
REPOSITORY = {"id": REPOSITORY_ID, "full_name": REPO, "default_branch": "main"}
VERSION = "0.1.0-rc.1"
TAG = f"v{VERSION}"
STEM = f"terraform-provider-webbpulse_{VERSION}_"
PK = providers.provider_pk("WebbPulse", "webbpulse")
BASE = "/api/v1/registry/providers"
ASSET_HOST = "https://release-assets.githubusercontent.com"
KEY_PARAMETER = "/webbpulse-terraform-test/provider-signing/public-key"
KEY_ID_PARAMETER = "/webbpulse-terraform-test/provider-signing/key-id"


@pytest.fixture(scope="module")
def signing() -> SigningKey:
    """The registry's signing key for the module."""
    return SigningKey.generate()


def build_release(key: SigningKey, *, version: str = VERSION, tamper: str | None = None) -> dict[str, bytes]:
    """The files GoReleaser's registry layout puts on a release, signed by `key`."""
    stem = f"terraform-provider-webbpulse_{version}_"
    files = {
        f"{stem}linux_amd64.zip": b"linux build",
        f"{stem}darwin_arm64.zip": b"darwin build",
        f"{stem}manifest.json": json.dumps({"version": 1, "metadata": {"protocol_versions": ["6.0"]}}).encode(),
    }
    sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())).encode()
    files[f"{stem}SHA256SUMS"] = sums
    files[f"{stem}SHA256SUMS.sig"] = key.sign(sums)
    if tamper:
        files[tamper] = b"tampered"
    return files


class Releases:
    """The release reads, answered from plain values in front of the shared fake GitHub."""

    def __init__(self, github: Any) -> None:
        """Wrap the shared fake so repository resolution keeps working."""
        self.github = github
        self.releases: dict[str, dict[str, bytes]] = {}
        self.drafts: set[str] = set()
        self.assets: dict[int, bytes] = {}
        self.asset_fetches = 0

    def add(self, tag: str, files: dict[str, bytes], *, draft: bool = False) -> None:
        """Publish a release at `tag` carrying `files`."""
        self.releases[tag] = files
        if draft:
            self.drafts.add(tag)

    def _release(self, tag: str) -> dict[str, Any]:
        """One release as the API renders it, numbering its assets."""
        assets = []
        for name, data in sorted(self.releases[tag].items()):
            asset_id = len(self.assets) + 1
            self.assets[asset_id] = data
            assets.append({"id": asset_id, "name": name, "size": len(data)})
        return {"tag_name": tag, "draft": tag in self.drafts, "prerelease": "-" in tag, "assets": assets}

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as GitHub would."""
        path = request.url.path
        if request.url.host == "release-assets.githubusercontent.com":
            assert "authorization" not in request.headers
            self.asset_fetches += 1
            return httpx.Response(200, content=self.assets[int(path.strip("/"))])
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/releases/tags/(.+)", path):
            tag = match.group(1)
            if tag not in self.releases or tag in self.drafts:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self._release(tag))
        if match := re.fullmatch(r"/repos/[^/]+/[^/]+/releases/assets/(\d+)", path):
            assert request.headers["accept"] == "application/octet-stream"
            return httpx.Response(302, headers={"Location": f"{ASSET_HOST}/{match.group(1)}"})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/releases", path):
            return httpx.Response(200, json=[self._release(tag) for tag in self.releases])
        return self.github.handle(request)


@pytest.fixture
def releases(monkeypatch: pytest.MonkeyPatch, github: Any) -> Releases:
    """A fake GitHub that also serves the provider repository's releases."""
    github.repositories.append(REPOSITORY)
    fake = Releases(github)

    def client() -> httpx.Client:
        """A client whose every request reaches the fake."""
        return httpx.Client(transport=httpx.MockTransport(fake.handle), follow_redirects=False)

    monkeypatch.setattr(service, "http_client", client)
    monkeypatch.setattr(tags, "http_client", client)
    return fake


@pytest.fixture
def signing_parameters(monkeypatch: pytest.MonkeyPatch, signing: SigningKey, settings: Any) -> SigningKey:
    """The signing key published to SSM the way the key generation workflow does, and named in the settings."""
    ssm = boto3.client("ssm", region_name="us-west-2")
    ssm.put_parameter(Name=KEY_PARAMETER, Value=signing.armored(), Type="String")
    ssm.put_parameter(Name=KEY_ID_PARAMETER, Value=signing.key_id, Type="String")
    monkeypatch.setenv("PROVIDER_SIGNING_KEY_PARAMETER", KEY_PARAMETER)
    monkeypatch.setenv("PROVIDER_SIGNING_KEY_ID_PARAMETER", KEY_ID_PARAMETER)
    monkeypatch.setattr(settings, "PROVIDER_SIGNING_KEY_PARAMETER", KEY_PARAMETER)
    monkeypatch.setattr(settings, "PROVIDER_SIGNING_KEY_ID_PARAMETER", KEY_ID_PARAMETER)
    return signing


@pytest.fixture
def provider(auth_client: Any, releases: Releases) -> dict[str, Any]:
    """The webbpulse provider, connected to its repository."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO, "import_releases": False})
    assert response.status_code == 201, response.text
    return response.json()


def release_record(version: str = VERSION, *, delivery: str = "r-1", **extra: Any) -> dict[str, Any]:
    """The SQS record the webhook route queues for a published release."""
    body = {
        "kind": RELEASE_KIND,
        "delivery": delivery,
        "event": "release",
        "repo": REPO,
        "repository_id": str(REPOSITORY_ID),
        "installation_id": INSTALLATION_ID,
        "actor": "octocat",
        "tag": f"v{version}",
        "version": version,
        **extra,
    }
    return {"body": json.dumps(body)}


def _row(settings: Any, version: str = VERSION) -> dict[str, Any]:
    """The stored version row of the webbpulse provider."""
    return repositories.registry(settings).get({"pk": PK, "sk": service.version_sk(version)}) or {}


@pytest.fixture
def reader(scoped_client: Any) -> Any:
    """A client holding only `registry:read`, the scope a TF_TOKEN key carries."""
    with scoped_client(REGISTRY_READ) as client:
        yield client


def test_the_real_release_signature_verifies_against_the_staging_key() -> None:
    """The v0.1.0-rc.1 SHA256SUMS signature made by GoReleaser and gpg verifies here."""
    key = (FIXTURES / "staging-signing-key.asc").read_text()
    sums = (FIXTURES / f"{STEM}SHA256SUMS").read_bytes()
    signature = (FIXTURES / f"{STEM}SHA256SUMS.sig").read_bytes()

    assert verify_detached(sums, signature, key) == "D4838D1C2609ADAB"
    with pytest.raises(SignatureInvalid):
        verify_detached(sums + b"x", signature, key)


def test_a_signature_by_another_key_is_refused(signing: SigningKey) -> None:
    """A signature naming a key the block does not hold never verifies."""
    other = SigningKey.generate()

    with pytest.raises(SignatureInvalid, match="does not hold"):
        verify_detached(b"data", other.sign(b"data"), signing.armored())


def test_only_a_published_semver_release_is_queued() -> None:
    """Drafts, other actions and non-semver tags are dropped at the webhook."""
    payload = {
        "action": "published",
        "release": {"tag_name": TAG, "draft": False},
        "repository": {"full_name": REPO, "id": REPOSITORY_ID},
        "installation": {"id": INSTALLATION_ID},
        "sender": {"login": "octocat"},
    }
    delivery = "0c4e1d7a-1111-2222-3333-444455556666"

    message = release_message("release", delivery, payload)

    assert message is not None
    assert (message["kind"], message["version"], message["tag"]) == (RELEASE_KIND, VERSION, TAG)
    assert parse_release_message({"body": json.dumps(message)})["repo"] == REPO
    assert release_message("release", delivery, {**payload, "action": "created"}) is None
    assert release_message("release", delivery, {**payload, "release": {"tag_name": "latest"}}) is None
    assert release_message("release", delivery, {**payload, "release": {"tag_name": TAG, "draft": True}}) is None
    with pytest.raises(MalformedDelivery):
        parse_release_message({"body": json.dumps({"kind": RELEASE_KIND})})


def test_connecting_needs_a_provider_repository(auth_client: Any, releases: Releases) -> None:
    """A repository not named terraform-provider-<type> is refused."""
    response = auth_client.post(BASE, json={"vcs_repo": "WebbPulse/terraform-aws-example"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "REGISTRY_INVALID_PROVIDER_NAME"


def test_connecting_twice_is_a_conflict(auth_client: Any, provider: dict[str, Any]) -> None:
    """One provider per address."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO})

    assert response.status_code == 409
    assert response.json()["error_code"] == "REGISTRY_PROVIDER_EXISTS"


def test_a_signed_release_publishes_and_terraform_can_install_it(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey, reader: Any
) -> None:
    """Every platform zip is stored, and the protocol serves what Terraform verifies."""
    files = build_release(signing_parameters)
    releases.add(TAG, files)

    outcome = dispatch.route_record(release_record(), settings=settings)

    assert outcome is None
    row = _row(settings)
    assert row["status"] == service.PUBLISHED, row.get("error")
    assert row["protocols"] == ["6.0"]
    assert {(item["os"], item["arch"]) for item in row["platforms"]} == {("linux", "amd64"), ("darwin", "arm64")}

    versions = reader.get("/v1/providers/WebbPulse/webbpulse/versions")
    assert versions.status_code == 200
    assert versions.json() == {
        "versions": [
            {
                "version": VERSION,
                "protocols": ["6.0"],
                "platforms": [{"os": "darwin", "arch": "arm64"}, {"os": "linux", "arch": "amd64"}],
            }
        ]
    }

    download = reader.get(f"/v1/providers/webbpulse/WEBBPULSE/{VERSION}/download/linux/amd64")
    assert download.status_code == 200
    body = download.json()
    assert body["filename"] == f"{STEM}linux_amd64.zip"
    assert body["shasum"] == hashlib.sha256(b"linux build").hexdigest()
    assert body["protocols"] == ["6.0"]
    assert body["signing_keys"]["gpg_public_keys"][0]["key_id"] == signing_parameters.key_id
    assert body["signing_keys"]["gpg_public_keys"][0]["ascii_armor"] == signing_parameters.armored()
    assert f"{STEM}SHA256SUMS" in body["shasums_url"]
    assert f"{STEM}SHA256SUMS.sig" in body["shasums_signature_url"]
    s3 = boto3.client("s3", region_name="us-west-2")
    stored = s3.get_object(
        Bucket=settings.ARTIFACTS_BUCKET,
        Key=providers.artifact_key("WebbPulse", "webbpulse", VERSION, body["filename"]),
    )
    assert stored["Body"].read() == b"linux build"


def test_a_published_version_is_not_fetched_again(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey
) -> None:
    """A redelivery changes nothing."""
    releases.add(TAG, build_release(signing_parameters))
    assert consumer.handle_release(release_record(), settings=settings) == {"WebbPulse/webbpulse": "published"}
    fetches = releases.asset_fetches

    assert consumer.handle_release(release_record(delivery="r-2"), settings=settings) == {
        "WebbPulse/webbpulse": consumer.SKIPPED
    }
    assert releases.asset_fetches == fetches


def test_a_release_signed_by_another_key_fails(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey, reader: Any
) -> None:
    """A staging signed prerelease in production fails just like this, and is never served."""
    releases.add(TAG, build_release(SigningKey.generate()))

    assert consumer.handle_release(release_record(), settings=settings) == {"WebbPulse/webbpulse": "failed"}
    assert "signature" in _row(settings)["error"]
    assert reader.get("/v1/providers/WebbPulse/webbpulse/versions").status_code == 404


def test_a_zip_that_does_not_match_its_checksum_fails(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey
) -> None:
    """Every stored zip is the one the signed list names."""
    releases.add(TAG, build_release(signing_parameters, tamper=f"{STEM}linux_amd64.zip"))

    assert consumer.handle_release(release_record(), settings=settings) == {"WebbPulse/webbpulse": "failed"}
    assert "does not match" in _row(settings)["error"]


def test_a_release_without_checksums_fails(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey
) -> None:
    """A release that is not GoReleaser's registry layout publishes nothing."""
    releases.add(TAG, {"notes.txt": b"hi"})

    assert consumer.handle_release(release_record(), settings=settings) == {"WebbPulse/webbpulse": "failed"}
    assert "SHA256SUMS" in _row(settings)["error"]


def test_no_signing_key_fails_the_version(settings: Any, provider: dict[str, Any], releases: Releases) -> None:
    """An environment with no signing key publishes nothing it cannot vouch for."""
    releases.add(TAG, build_release(SigningKey.generate()))

    assert consumer.handle_release(release_record(), settings=settings) == {"WebbPulse/webbpulse": "failed"}
    assert "signing key" in _row(settings)["error"]


def test_an_unconnected_repository_publishes_nothing(settings: Any, releases: Releases) -> None:
    """A release in a repository no provider is connected to is acknowledged."""
    assert consumer.handle_release(release_record(), settings=settings) == {}


def test_a_sync_queues_each_unpublished_release_for_that_provider(
    ingest_queue: str, settings: Any, auth_client: Any, releases: Releases, signing_parameters: SigningKey
) -> None:
    """Drafts and non-semver tags are skipped, and a published version is not queued again."""
    releases.add(TAG, build_release(signing_parameters))
    releases.add("v0.2.0", build_release(signing_parameters, version="0.2.0"))
    releases.add("v0.3.0", build_release(signing_parameters, version="0.3.0"), draft=True)
    releases.add("nightly", {})
    response = auth_client.post(BASE, json={"vcs_repo": REPO})
    assert response.status_code == 201, response.text
    [sync] = drain(ingest_queue)
    assert json.loads(sync["body"])["kind"] == providers.SYNC_KIND

    dispatch.route_record(sync, settings=settings)
    queued = drain(ingest_queue)

    assert sorted(json.loads(record["body"])["version"] for record in queued) == ["0.1.0-rc.1", "0.2.0"]
    assert {json.loads(record["body"])["provider"] for record in queued} == {PK}
    for record in queued:
        dispatch.route_record(record, settings=settings)
    assert _row(settings, "0.2.0")["status"] == service.PUBLISHED

    resync = auth_client.post(f"{BASE}/WebbPulse/webbpulse/resync")
    assert resync.status_code == 202
    dispatch.route_record(drain(ingest_queue)[0], settings=settings)
    assert drain(ingest_queue) == []


def test_listing_and_reading_a_provider(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey, reader: Any
) -> None:
    """The SPA sees every version with its status."""
    releases.add(TAG, build_release(signing_parameters))
    consumer.handle_release(release_record(), settings=settings)

    listed = reader.get(BASE)
    one = reader.get(f"{BASE}/WebbPulse/webbpulse")

    assert listed.status_code == 200
    assert [item["source"] for item in listed.json()["providers"]] == ["WebbPulse/webbpulse"]
    assert one.json()["versions"][0]["status"] == "published"
    assert one.json()["versions"][0]["key_id"] == signing_parameters.key_id


def test_deleting_a_provider_removes_its_files(
    settings: Any, auth_client: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey
) -> None:
    """The provider row, the version rows and every stored file go."""
    releases.add(TAG, build_release(signing_parameters))
    consumer.handle_release(release_record(), settings=settings)

    response = auth_client.delete(f"{BASE}/WebbPulse/webbpulse")

    assert response.status_code == 204
    assert auth_client.get(f"{BASE}/WebbPulse/webbpulse").status_code == 404
    listed = boto3.client("s3", region_name="us-west-2").list_objects_v2(
        Bucket=settings.ARTIFACTS_BUCKET, Prefix=providers.PROVIDERS_PREFIX
    )
    assert listed.get("KeyCount", 0) == 0


def test_the_protocol_needs_registry_read(app: Any, scoped_client: Any) -> None:
    """No credential is a 401 and a key without `registry:read` a 403."""
    from fastapi.testclient import TestClient

    with TestClient(app) as anonymous:
        assert anonymous.get("/v1/providers/WebbPulse/webbpulse/versions").status_code == 401
    with scoped_client(WORKSPACES_READ) as client:
        assert client.get("/v1/providers/WebbPulse/webbpulse/versions").status_code == 403
