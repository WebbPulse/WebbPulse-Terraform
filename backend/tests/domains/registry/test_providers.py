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


def test_a_manifest_missing_from_the_checksums_fails(
    settings: Any, provider: dict[str, Any], releases: Releases, signing_parameters: SigningKey
) -> None:
    """An unsigned manifest could change the protocols Terraform is told to speak."""
    files = build_release(signing_parameters)
    files[f"{STEM}manifest.json"] = json.dumps({"version": 1, "metadata": {"protocol_versions": ["5.0"]}}).encode()
    sums = b"".join(
        line + b"\n" for line in files[f"{STEM}SHA256SUMS"].splitlines() if not line.endswith(b"manifest.json")
    )
    files[f"{STEM}SHA256SUMS"] = sums
    files[f"{STEM}SHA256SUMS.sig"] = signing_parameters.sign(sums)
    releases.add(TAG, files)

    assert consumer.handle_release(release_record(), settings=settings) == {"WebbPulse/webbpulse": "failed"}
    assert "not listed" in _row(settings)["error"]


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


def upload(
    settings: Any,
    files: dict[str, bytes],
    *,
    version: str = VERSION,
    upload_id: str = "123-1",
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upload a release the way the release workflow's OIDC role does, returning the record EventBridge queues."""
    s3 = boto3.client("s3", region_name="us-west-2")
    folder = f"{consumer.UPLOADS_PREFIX}webbpulse/webbpulse/{version}/{upload_id}/"
    for name, data in files.items():
        s3.put_object(Bucket=settings.ARTIFACTS_BUCKET, Key=folder + name, Body=data)
    body = request if request is not None else {"repository": REPO, "tag": f"v{version}", "actor": "octocat"}
    s3.put_object(Bucket=settings.ARTIFACTS_BUCKET, Key=folder + "upload.json", Body=json.dumps(body).encode())
    message = {"kind": consumer.UPLOAD, "bucket": settings.ARTIFACTS_BUCKET, "key": folder + "upload.json"}
    return {"body": json.dumps(message)}


def _uploads_left(settings: Any) -> int:
    """How many objects remain under the uploads prefix."""
    listed = boto3.client("s3", region_name="us-west-2").list_objects_v2(
        Bucket=settings.ARTIFACTS_BUCKET, Prefix=consumer.UPLOADS_PREFIX
    )
    return int(listed.get("KeyCount", 0))


def test_an_upload_publishes_without_an_app_and_terraform_can_install_it(
    settings: Any, signing_parameters: SigningKey, reader: Any
) -> None:
    """A signed upload creates the provider, publishes the version and clears its folder."""
    dispatch.route_record(upload(settings, build_release(signing_parameters)), settings=settings)

    row = _row(settings)
    assert row["status"] == service.PUBLISHED, row.get("error")
    assert row["delivery"] == "upload-123-1"
    provider_row = repositories.registry(settings).get(providers.provider_row_key("WebbPulse", "webbpulse")) or {}
    assert provider_row["vcs_repo"] == REPO
    assert "vcs_installation_id" not in provider_row
    assert _uploads_left(settings) == 0
    download = reader.get(f"/v1/providers/WebbPulse/webbpulse/{VERSION}/download/linux/amd64")
    assert download.status_code == 200
    assert download.json()["shasum"] == hashlib.sha256(b"linux build").hexdigest()


def test_an_upload_of_a_published_version_is_skipped(settings: Any, signing_parameters: SigningKey) -> None:
    """A second upload of the same version changes nothing and is still cleared."""
    files = build_release(signing_parameters)
    assert consumer.handle_upload(upload(settings, files), settings=settings) == service.PUBLISHED

    assert consumer.handle_upload(upload(settings, files, upload_id="124-1"), settings=settings) == consumer.SKIPPED
    assert _row(settings)["delivery"] == "upload-123-1"
    assert _uploads_left(settings) == 0


def test_an_upload_signed_by_another_key_fails(settings: Any, signing_parameters: SigningKey, reader: Any) -> None:
    """The environment's own key is still the only one that publishes."""
    record = upload(settings, build_release(SigningKey.generate()))

    assert consumer.handle_upload(record, settings=settings) == service.FAILED
    assert "signature" in _row(settings)["error"]
    assert reader.get("/v1/providers/WebbPulse/webbpulse/versions").status_code == 404
    assert _uploads_left(settings) == 0


def test_an_upload_missing_a_listed_zip_fails(settings: Any, signing_parameters: SigningKey) -> None:
    """Every platform the signed list names must be in the folder."""
    files = build_release(signing_parameters)
    del files[f"{STEM}darwin_arm64.zip"]

    assert consumer.handle_upload(upload(settings, files), settings=settings) == service.FAILED
    assert "darwin_arm64.zip" in _row(settings)["error"]


@pytest.mark.parametrize(
    "request_body",
    [
        {"repository": "WebbPulse/terraform-provider-other", "tag": TAG},
        {"repository": REPO, "tag": "v9.9.9"},
        {"repository": REPO},
    ],
)
def test_an_upload_that_disagrees_with_its_folder_is_refused(
    settings: Any, signing_parameters: SigningKey, request_body: dict[str, Any]
) -> None:
    """The repository and tag must name the address and version the folder sits under."""
    record = upload(settings, build_release(signing_parameters), request=request_body)

    assert consumer.handle_upload(record, settings=settings) == service.FAILED
    assert _row(settings) == {}
    assert _uploads_left(settings) == 0


def test_an_upload_outside_the_uploads_prefix_is_ignored(settings: Any) -> None:
    """Only keys shaped like an upload folder's upload.json are read."""
    record = {
        "body": json.dumps({"kind": consumer.UPLOAD, "bucket": settings.ARTIFACTS_BUCKET, "key": "ingest/x.tar.gz"})
    }
    other = {"body": json.dumps({"kind": consumer.UPLOAD, "bucket": "elsewhere", "key": "registry/x"})}

    assert consumer.handle_upload(record, settings=settings) == consumer.SKIPPED
    assert consumer.handle_upload(other, settings=settings) == consumer.SKIPPED
    with pytest.raises(MalformedDelivery):
        consumer.parse_upload_message({"body": json.dumps({"kind": consumer.UPLOAD})})


def test_connecting_adopts_a_provider_uploads_created(
    settings: Any, auth_client: Any, releases: Releases, signing_parameters: SigningKey
) -> None:
    """Connecting through the App later binds the upload-only provider and keeps its versions."""
    consumer.handle_upload(upload(settings, build_release(signing_parameters)), settings=settings)

    response = auth_client.post(BASE, json={"vcs_repo": REPO, "import_releases": False})

    assert response.status_code == 201, response.text
    provider_row = repositories.registry(settings).get(providers.provider_row_key("WebbPulse", "webbpulse")) or {}
    assert provider_row["vcs_installation_id"]
    assert _row(settings)["status"] == service.PUBLISHED
    assert auth_client.post(BASE, json={"vcs_repo": REPO}).status_code == 409


ISSUER = "https://api.terraform.test/api/auth"
AUDIENCE = "webbpulse-terraform-test-api"
DEVICE_AUDIENCE = f"{ISSUER}/device"


class _Jwks:
    """A key set client serving one RSA key, standing in for the issuer's JWKS."""

    def __init__(self, public_key: Any) -> None:
        """Serve `public_key` for every `kid`."""
        self.public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> Any:
        """The key the verifier checks the signature with."""
        return type("SigningKey", (), {"key": self.public_key})()


@pytest.fixture(scope="module")
def token_key() -> Any:
    """The issuer's RSA signing key for the module."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch, token_key: Any) -> dict[str, bool]:
    """Verify access tokens against the test key, with device grants live unless flipped."""
    from webbpulse.identity.verifier import JwksVerifier

    from app.domains.registry import protocol_router

    verifier = JwksVerifier(issuer=ISSUER, audience=[AUDIENCE, DEVICE_AUDIENCE], client=_Jwks(token_key.public_key()))
    grants = {"live": True}
    monkeypatch.setattr(protocol_router, "access_token_verifier", lambda: verifier)
    monkeypatch.setattr(protocol_router, "device_grant_liveness", lambda: lambda _claims: grants["live"])
    return grants


def access_token(key: Any, *, scope: str = REGISTRY_READ, device: bool = True, **overrides: Any) -> str:
    """A signed access token, a `wp-tf login` device token unless `device` is false."""
    import time

    import jwt

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": DEVICE_AUDIENCE if device else AUDIENCE,
        "sub": "user-device",
        "iat": now,
        "exp": now + 600,
        "typ": "access",
        "scope": scope,
    }
    if device:
        claims["grant"] = "device"
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def published_upload(settings: Any, signing_parameters: SigningKey) -> None:
    """The provider published from an upload, so the protocol has a version to serve."""
    dispatch.route_record(upload(settings, build_release(signing_parameters)), settings=settings)
    assert _row(settings)["status"] == service.PUBLISHED


DOWNLOAD = f"/v1/providers/WebbPulse/webbpulse/{VERSION}/download/linux/amd64"


@pytest.mark.parametrize("device", [True, False], ids=["device token", "session token"])
def test_a_persons_access_token_with_registry_read_downloads_a_provider(
    app: Any, issuer: dict[str, bool], token_key: Any, published_upload: None, device: bool
) -> None:
    """A `wp-tf login` or browser token holding `registry:read` gets the download and its checksum list."""
    from fastapi.testclient import TestClient

    headers = {"Authorization": f"Bearer {access_token(token_key, device=device)}"}
    with TestClient(app, headers=headers) as client:
        versions = client.get("/v1/providers/WebbPulse/webbpulse/versions")
        download = client.get(DOWNLOAD)

    assert versions.status_code == 200, versions.text
    assert download.status_code == 200, download.text
    body = download.json()
    assert body["shasum"] == hashlib.sha256(b"linux build").hexdigest()
    assert f"{STEM}SHA256SUMS" in body["shasums_url"]
    assert "X-Amz-Signature" in body["shasums_url"]


def test_an_access_token_without_registry_read_is_forbidden(
    app: Any, issuer: dict[str, bool], token_key: Any, published_upload: None
) -> None:
    """The token verifies, so the refusal is the scope 403."""
    from fastapi.testclient import TestClient

    headers = {"Authorization": f"Bearer {access_token(token_key, scope=WORKSPACES_READ)}"}
    with TestClient(app, headers=headers) as client:
        assert client.get(DOWNLOAD).status_code == 403


def test_a_revoked_device_grant_is_refused(
    app: Any, issuer: dict[str, bool], token_key: Any, published_upload: None
) -> None:
    """`wp-tf logout` or a revoked session ends registry access too."""
    from fastapi.testclient import TestClient

    issuer["live"] = False
    headers = {"Authorization": f"Bearer {access_token(token_key)}"}
    with TestClient(app, headers=headers) as client:
        assert client.get(DOWNLOAD).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [{"exp": 1}, {"iss": "https://elsewhere.test"}, {"aud": "another-api"}, {"typ": "mfa"}],
    ids=["expired", "another issuer", "another audience", "not an access token"],
)
def test_an_invalid_access_token_is_unauthorized(
    app: Any, issuer: dict[str, bool], token_key: Any, published_upload: None, overrides: dict[str, Any]
) -> None:
    """Every verification failure is the same 401."""
    from fastapi.testclient import TestClient

    headers = {"Authorization": f"Bearer {access_token(token_key, **overrides)}"}
    with TestClient(app, headers=headers) as client:
        assert client.get(DOWNLOAD).status_code == 401


def test_a_token_signed_by_another_key_is_unauthorized(
    app: Any, issuer: dict[str, bool], published_upload: None
) -> None:
    """A forged signature is refused."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from fastapi.testclient import TestClient

    forged = access_token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    with TestClient(app, headers={"Authorization": f"Bearer {forged}"}) as client:
        assert client.get(DOWNLOAD).status_code == 401


def test_an_access_token_is_refused_where_no_issuer_is_configured(
    app: Any, token_key: Any, published_upload: None
) -> None:
    """Without `IDENTITY_ISSUER` there is nothing to verify against, so it fails closed."""
    from fastapi.testclient import TestClient

    headers = {"Authorization": f"Bearer {access_token(token_key)}"}
    with TestClient(app, headers=headers) as client:
        assert client.get(DOWNLOAD).status_code == 401


def test_the_verifier_follows_the_function_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The issuer, session audience and device audience come from the identity environment alone."""
    from app.common.composition.settings import get_settings
    from app.domains.registry import protocol_router

    monkeypatch.setenv("IDENTITY_ISSUER", ISSUER)
    monkeypatch.setenv("IDENTITY_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("IDENTITY_DEVICE_GRANT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        verifier = protocol_router.access_token_verifier()
        assert verifier is not None
        assert verifier.jwks_uri == f"{ISSUER}/.well-known/jwks.json"
        assert verifier._audience == [AUDIENCE, DEVICE_AUDIENCE]  # pyright: ignore[reportPrivateUsage]
        monkeypatch.delenv("IDENTITY_AUDIENCE")
        assert protocol_router.access_token_verifier() is None
    finally:
        get_settings.cache_clear()
