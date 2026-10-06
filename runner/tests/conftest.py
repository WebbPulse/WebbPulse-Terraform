"""Fixtures: a mocked AWS environment, a fake engine on PATH and an httpx mock transport."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

os.environ.update(
    {
        "AWS_DEFAULT_REGION": "us-west-2",
        "AWS_REGION": "us-west-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
    }
)

import boto3  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from app import install  # noqa: E402
from app.main import Clients  # noqa: E402
from app.models import RunnerEnv  # noqa: E402

LOG_GROUP = "/webbpulse-terraform/staging/runner"
RUN_ID = "run-01JBTESTRUNIDAAAAAAAAAAAA"
WORKSPACE_ID = "ws-01JBTESTWORKSPACEAAAAAAAA"
API_BASE_URL = "https://staging.terraform.webbpulse.com"
RUN_TOKEN = "run-token-do-not-log-abcdefghij"
TASK_TOKEN = "task-token-do-not-log-klmnopqrst"
SECRET_TFVAR = "super-secret-database-password-1234"
SECRET_ENVVAR = "secret-provider-credential-567890abc"
SECRET_HCL_TFVAR = '["secret-list-member-abcdefghij", "secret-list-member-klmnopqrst"]'
REGISTRY_TOKEN = "wpk_registry-token-do-not-log-abcdefghij"
REGISTRY_HOST = "staging.terraform-e2e.webbpulse.com"
"""A sensitive variable whose value is an HCL expression. The expression is the
secret, so it is the expression the redactor has to mask."""

PLAN_JSON_WITH_CHANGES = {
    "format_version": "1.2",
    "resource_changes": [
        {"address": "aws_s3_bucket.a", "change": {"actions": ["create"]}},
        {"address": "aws_s3_bucket.b", "change": {"actions": ["update"]}},
        {"address": "aws_s3_bucket.c", "change": {"actions": ["delete"]}},
        {"address": "aws_s3_bucket.d", "change": {"actions": ["no-op"]}},
        {"address": "aws_s3_bucket.e", "change": {"actions": ["create", "delete"]}},
    ],
}

PLAN_JSON_NO_CHANGES: dict[str, Any] = {"format_version": "1.2", "resource_changes": []}


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A moto backed AWS environment with the runner log group in place."""
    with mock_aws():
        boto3.client("logs", region_name="us-west-2").create_log_group(logGroupName=LOG_GROUP)
        yield None


@pytest.fixture
def run_role_arn(aws: None) -> str:
    """The workspace run role the vended provider keys are a session of."""
    return "arn:aws:iam::870550636948:role/webbpulse-terraform-staging-workspace-e2e"


PROVIDER_ACCESS_KEY_ID = "ASIAPROVIDERKEY00001"
PROVIDER_SECRET_ACCESS_KEY = "provider-secret-access-key-abcdef0123456789"
PROVIDER_SESSION_TOKEN = "provider-session-token-abcdef0123456789"
STATE_ACCESS_KEY_ID = "ASIASTATEKEY00000001"
STATE_SECRET_ACCESS_KEY = "state-secret-access-key-abcdef0123456789"
STATE_SESSION_TOKEN = "state-session-token-abcdef0123456789"
"""The vended keys a bundle carries, which must reach the engine and never a log."""


def build_config_tarball(*names: str) -> bytes:
    """A terraform config as a gzipped tarball, one `main.tf` per given path."""
    buffer = io.BytesIO()
    body = 'terraform {\n  required_version = ">= 1.11"\n}\n'
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for name in names or ("main.tf",):
            info = tarfile.TarInfo(name)
            payload = body.encode()
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture
def config_tarball() -> bytes:
    """A minimal terraform config as a gzipped tarball."""
    return build_config_tarball("main.tf")


@pytest.fixture
def nested_config_tarball() -> bytes:
    """A config whose terraform lives in `infra/`, as a working directory needs."""
    return build_config_tarball("main.tf", "infra/main.tf")


BAKED_VERSION = "1.16.4"
"""The version the fake engine on PATH reports, standing in for the image's baked release."""

SECRET_OUTPUT = "sensitive-output-value-uvwxyz0123"


def bundle_payload(
    run_role_arn: str,
    *,
    engine: str = "terraform",
    plan_get_url: str | None = None,
    workdir_get_url: str | None = None,
    engine_version: str = BAKED_VERSION,
) -> dict[str, Any]:
    """A bundle the runs domain would serve, carrying sensitive variable values."""
    return {
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "engine": engine,
        "engine_version": engine_version,
        "config_url": "https://artifacts.example.invalid/configs/config.tar.gz?sig=1",
        "backend": {
            "bucket": "webbpulse-terraform-staging-state",
            "key": f"workspaces/{WORKSPACE_ID}/terraform.tfstate",
            "region": "us-west-2",
            "kms_key_id": "arn:aws:kms:us-west-2:870550636948:key/11111111-2222-3333-4444-555555555555",
            "credentials": {
                "access_key_id": STATE_ACCESS_KEY_ID,
                "secret_access_key": STATE_SECRET_ACCESS_KEY,
                "session_token": STATE_SESSION_TOKEN,
                "expiration": "2026-09-26T13:00:00+00:00",
            },
        },
        "run_role_arn": run_role_arn,
        "aws_credentials": {
            "access_key_id": PROVIDER_ACCESS_KEY_ID,
            "secret_access_key": PROVIDER_SECRET_ACCESS_KEY,
            "session_token": PROVIDER_SESSION_TOKEN,
            "expiration": "2026-09-26T13:00:00+00:00",
        },
        "environment_variables": {"PROVIDER_TOKEN": SECRET_ENVVAR},
        "terraform_variables": {"db_password": SECRET_TFVAR, "instance_count": 2},
        "artifacts": {"plan_get_url": plan_get_url, "workdir_get_url": workdir_get_url},
    }


ARTIFACT_OBJECTS: dict[str, tuple[str, str]] = {
    "plan": ("/runs/plan.tfplan", "application/octet-stream"),
    "plan_json": ("/runs/plan.json", "application/json"),
    "workdir": ("/runs/workdir.tar.gz", "application/gzip"),
    "log": ("/runs/plan.log", "text/plain"),
    "outputs_json": ("/runs/outputs.json", "application/json"),
}
"""The path and signed content type the fake API mints an upload for, per kind."""


class ApiRecorder:
    """Records what the runner sent, so assertions can read the posted result back."""

    def failure_names(self) -> list[str]:
        """The error names of the failures the runner posted, in order."""
        return [str(result["error_name"]) for result in self.phase_results if "error_name" in result]

    def __init__(self) -> None:
        self.phase_results: list[dict[str, Any]] = []
        self.uploads: dict[str, bytes] = {}
        self.upload_headers: dict[str, dict[str, str]] = {}
        self.upload_requests: list[dict[str, Any]] = []
        self.bundle_requests = 0
        self.heartbeats: list[dict[str, Any]] = []
        self.heartbeat_headers: list[dict[str, str]] = []
        self.credential_requests: list[dict[str, Any]] = []


REFRESHED_ACCESS_KEY_ID = "ASIAREFRESHEDKEY0001"
REFRESHED_SECRET_ACCESS_KEY = "refreshed-secret-access-key-abcdef0123456789"
REFRESHED_SESSION_TOKEN = "refreshed-session-token-abcdef0123456789"
REFRESHED_STATE_SECRET_ACCESS_KEY = "refreshed-state-secret-access-key-abcdef01234"
REFRESHED_STATE_SESSION_TOKEN = "refreshed-state-session-token-abcdef01234567"


GCP_IDENTITY_TOKEN = "eyJhbGciOiJSUzI1NiJ9.gcp-identity-claims.gcp-signature"
AZURE_IDENTITY_TOKEN = "eyJhbGciOiJSUzI1NiJ9.azure-identity-claims.azure-signature"
GCP_AUDIENCE = "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/p"
GCP_SERVICE_ACCOUNT = "runs@example.iam.gserviceaccount.com"
AZURE_CLIENT_ID = "00000000-0000-0000-0000-000000000001"


def workload_identity_payload(
    gcp_token: str = GCP_IDENTITY_TOKEN,
    azure_token: str = AZURE_IDENTITY_TOKEN,
    expiration: str = "2099-01-01T00:00:00Z",
) -> dict[str, Any]:
    """The Google and Azure identity tokens a workspace asking for both is given."""
    return {
        "gcp": {
            "token": gcp_token,
            "expiration": expiration,
            "audience": GCP_AUDIENCE,
            "service_account_email": GCP_SERVICE_ACCOUNT,
        },
        "azure": {"token": azure_token, "expiration": expiration, "client_id": AZURE_CLIENT_ID},
    }


def refreshed_payload(expiration: str = "2099-01-01T00:00:00+00:00") -> dict[str, Any]:
    """What `POST /runs/{id}/credentials` answers with: both sessions, freshly vended."""
    return {
        "aws_credentials": {
            "access_key_id": REFRESHED_ACCESS_KEY_ID,
            "secret_access_key": REFRESHED_SECRET_ACCESS_KEY,
            "session_token": REFRESHED_SESSION_TOKEN,
            "expiration": expiration,
        },
        "backend_credentials": {
            "access_key_id": "ASIAREFRESHEDSTATE01",
            "secret_access_key": REFRESHED_STATE_SECRET_ACCESS_KEY,
            "session_token": REFRESHED_STATE_SESSION_TOKEN,
            "expiration": expiration,
        },
    }


def make_transport(
    bundle: dict[str, Any] | None,
    config_tarball: bytes,
    recorder: ApiRecorder,
    *,
    bundle_status: int = 200,
    bundle_body: dict[str, Any] | None = None,
    plan_bytes: bytes = b"fake-plan",
    upload_request_status: int = 200,
    upload_status: int = 200,
    releases: dict[str, bytes] | None = None,
    refused_uploads: frozenset[str] = frozenset(),
    heartbeat_status: int = 204,
    credentials_status: int = 200,
    workdir_tarball: bytes | None = None,
) -> httpx.MockTransport:
    """An httpx transport serving the bundle, the config tarball and the artifact uploads.

    The upload route mints a URL that names the declared size, and the PUT handler
    refuses a body whose length does not match it, so a runner that sent the wrong
    `Content-Length` fails here the way S3 fails it. `releases` serves engine
    release files by URL, `refused_uploads` names artifact kinds whose upload
    request is refused, `bundle_body` is a refused bundle's error body and
    `heartbeat_status` is what every heartbeat is answered with and
    `credentials_status` what every credential refresh is. `workdir_tarball` is
    the planned working directory an apply restores.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host in ("releases.hashicorp.com", "github.com"):
            served = (releases or {}).get(str(request.url))
            return httpx.Response(404) if served is None else httpx.Response(200, content=served)
        if path.endswith("/runner-token"):
            return httpx.Response(200, json={"run_token": RUN_TOKEN})
        if path.endswith("/bundle"):
            recorder.bundle_requests += 1
            if bundle_status != 200 or bundle is None:
                return httpx.Response(bundle_status, json=bundle_body or {"detail": "nope"})
            return httpx.Response(200, json=bundle)
        if path.endswith("/artifact-uploads"):
            payload = json.loads(request.content)
            recorder.upload_requests.append(payload)
            if str(payload["artifact"]) in refused_uploads:
                return httpx.Response(422, json={"detail": "unknown artifact"})
            if upload_request_status != 200:
                return httpx.Response(upload_request_status, json={"detail": "nope"})
            object_path, content_type = ARTIFACT_OBJECTS[str(payload["artifact"])]
            size = int(payload["size_bytes"])
            return httpx.Response(
                200,
                json={
                    "url": f"https://artifacts.example.invalid{object_path}?sig=1&len={size}",
                    "headers": {"Content-Type": content_type, "Content-Length": str(size)},
                    "expires_in": 3600,
                },
            )
        if path.endswith("/heartbeat"):
            recorder.heartbeats.append(json.loads(request.content))
            recorder.heartbeat_headers.append(dict(request.headers))
            if heartbeat_status >= 400:
                return httpx.Response(
                    heartbeat_status, json={"detail": {"error_code": "PHASE_TASK_ENDED", "message": "ended"}}
                )
            return httpx.Response(heartbeat_status)
        if path.endswith("/credentials"):
            recorder.credential_requests.append(json.loads(request.content))
            if credentials_status != 200:
                return httpx.Response(
                    credentials_status, json={"detail": {"error_code": "PHASE_MISMATCH", "message": "moved on"}}
                )
            return httpx.Response(200, json=refreshed_payload())
        if path.endswith("/phase-result"):
            recorder.phase_results.append(json.loads(request.content))
            return httpx.Response(204)
        if "config.tar.gz" in path:
            return httpx.Response(200, content=config_tarball)
        if request.method == "PUT":
            if upload_status != 200:
                return httpx.Response(upload_status)
            signed = request.url.params.get("len")
            if signed is not None and int(signed) != len(request.content):
                return httpx.Response(403, json={"detail": "content length does not match the signature"})
            recorder.uploads[path] = request.content
            recorder.upload_headers[path] = dict(request.headers)
            return httpx.Response(200)
        if path.endswith("plan.tfplan"):
            return httpx.Response(200, content=plan_bytes)
        if path.endswith("workdir.tar.gz") and workdir_tarball is not None:
            return httpx.Response(200, content=workdir_tarball)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def fake_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Installs a fake `terraform` and `tofu` on PATH whose behaviour the test chooses."""
    bin_directory = tmp_path / "fake-bin"
    bin_directory.mkdir()

    def install(
        *,
        plan_exit: int = 2,
        plan_json: dict[str, Any] | None = None,
        apply_exit: int = 0,
        init_exit: int = 0,
        plan_sleep: float = 0.0,
        echo_environment: bool = True,
        version: str = BAKED_VERSION,
        directory: Path | None = None,
        plan_link: str | None = None,
        show_output: str | None = None,
        plan_writes: tuple[str, ...] = (),
        apply_reads: tuple[str, ...] = (),
    ) -> Path:
        document = json.dumps(PLAN_JSON_WITH_CHANGES if plan_json is None else plan_json)
        shown = document if show_output is None else show_output
        script = f"""#!/usr/bin/env python3
import json, os, sys, time

SUBCOMMAND = sys.argv[1] if len(sys.argv) > 1 else ""
ECHO = {echo_environment!r}
VERSION = {version!r}
PLAN_LINK = {plan_link!r}
PLAN_WRITES = {plan_writes!r}
APPLY_READS = {apply_reads!r}

if SUBCOMMAND == "version":
    if sys.argv[2:] == ["-json"]:
        print(json.dumps({{"terraform_version": VERSION}}))
    else:
        print("Terraform v" + VERSION)
    sys.exit(0)
for key in sorted(os.environ):
    if SUBCOMMAND == "show" and key.startswith("WEBBPULSE_TF_"):
        continue
    if key.startswith(("TF_TOKEN_", "WEBBPULSE_TF_")) or key == "TF_CLI_CONFIG_FILE":
        print(SUBCOMMAND + " holds " + key + "=" + os.environ[key])
if SUBCOMMAND == "init":
    print("Initializing the backend...")
    if "TF_CLI_CONFIG_FILE" in os.environ:
        for line in open(os.environ["TF_CLI_CONFIG_FILE"]).read().splitlines():
            print("cli config " + line)
    if ECHO:
        for key in sorted(os.environ):
            if key.startswith(("TF_VAR_", "PROVIDER_", "AWS_", "GOOGLE_", "ARM_")):
                print("env " + key + "=" + os.environ[key])
        for key in ("ARM_OIDC_TOKEN_FILE_PATH",):
            if key in os.environ:
                print("token file " + open(os.environ[key]).read())
        if "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
            source = json.load(open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]))["credential_source"]["file"]
            print("token file " + open(source).read())
        for name in sorted(os.listdir(".")):
            if name.endswith(".tfvars.json") or name.endswith(".tfvars"):
                print("tfvars file " + name)
                if name.endswith(".tfvars"):
                    for line in open(name).read().splitlines():
                        print("tfvars line " + line)
    sys.exit({init_exit})
if SUBCOMMAND == "plan":
    print("plan arguments " + " ".join(sys.argv[2:]))
    print("Terraform used the selected providers to generate the plan.", flush=True)
    try:
        time.sleep({plan_sleep!r})
    except KeyboardInterrupt:
        print("Interrupt received. Gracefully shutting down...", flush=True)
        sys.exit(1)
    for written in PLAN_WRITES:
        os.makedirs(os.path.dirname(written) or ".", exist_ok=True)
        open(written, "w").write("generated " + written)
    if PLAN_LINK:
        os.symlink(PLAN_LINK, "plan.tfplan")
    else:
        open("plan.tfplan", "w").write("fake-plan")
    sys.exit({plan_exit})
if SUBCOMMAND == "show":
    shown_plan = sys.argv[-1]
    if os.path.basename(os.path.dirname(shown_plan)) != "artifacts" or open(shown_plan).read() != "fake-plan":
        print("show was not given the sealed plan", file=sys.stderr)
        sys.exit(1)
    print({shown!r})
    sys.exit(0)
if SUBCOMMAND == "apply":
    print("apply arguments " + " ".join(sys.argv[2:]))
    for read in APPLY_READS:
        if not os.path.isfile(read) or open(read).read() != "generated " + read:
            print("apply is missing " + read, file=sys.stderr)
            sys.exit(1)
    print("Apply complete! Resources: 1 added, 0 changed, 0 destroyed.")
    sys.exit({apply_exit})
if SUBCOMMAND == "output":
    print(json.dumps({{
        "pet_name": {{"sensitive": False, "type": "string", "value": "lucky-horse"}},
        "secret": {{"sensitive": True, "type": "string", "value": {SECRET_OUTPUT!r}}},
    }}))
    sys.exit(0)
print("unexpected subcommand " + SUBCOMMAND, file=sys.stderr)
sys.exit(1)
"""
        target_directory = directory or bin_directory
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in ("terraform", "tofu"):
            target = target_directory / name
            target.write_text(script)
            target.chmod(0o755)
        if directory is None:
            monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
        return target_directory

    return install


@dataclass(frozen=True)
class ReleaseSigner:
    """A throwaway OpenPGP key standing in for a publisher's release key."""

    home: Path
    fingerprint: str

    def sign(self, payload: bytes) -> bytes:
        """A binary detached signature over `payload`, the form the publishers serve."""
        completed = subprocess.run(  # noqa: S603
            ["gpg", "--homedir", str(self.home), "--batch", "--yes", "--local-user", self.fingerprint, "--detach-sign"],
            input=payload,
            capture_output=True,
            check=True,
        )
        return completed.stdout

    def export(self, target: Path) -> None:
        """Write the public key as a binary keyring, as the image bakes it."""
        completed = subprocess.run(  # noqa: S603
            ["gpg", "--homedir", str(self.home), "--batch", "--export", self.fingerprint],
            capture_output=True,
            check=True,
        )
        target.write_bytes(completed.stdout)


def make_signer(home: Path) -> ReleaseSigner:
    """Generate an unprotected Ed25519 signing key in a fresh GnuPG home."""
    home.mkdir(mode=0o700, parents=True)
    subprocess.run(  # noqa: S603
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            "Release Test <release-test@example.invalid>",
            "ed25519",
            "sign",
            "never",
        ],
        capture_output=True,
        check=True,
    )
    listing = subprocess.run(  # noqa: S603
        ["gpg", "--homedir", str(home), "--batch", "--with-colons", "--list-keys"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    return ReleaseSigner(home=home, fingerprint=fingerprint)


@pytest.fixture(scope="session")
def release_signer(tmp_path_factory: pytest.TempPathFactory) -> ReleaseSigner:
    """The key every test release is signed with."""
    return make_signer(tmp_path_factory.mktemp("release-key") / "gnupg")


@pytest.fixture(scope="session")
def release_keyrings(tmp_path_factory: pytest.TempPathFactory, release_signer: ReleaseSigner) -> Path:
    """A keyring directory baking the test key for both engines."""
    directory = tmp_path_factory.mktemp("keyrings")
    for engine in ("terraform", "tofu"):
        release_signer.export(directory / f"{engine}.gpg")
    return directory


@pytest.fixture(autouse=True)
def pinned_release_key(monkeypatch: pytest.MonkeyPatch, release_signer: ReleaseSigner, release_keyrings: Path) -> None:
    """Pin the test key in place of the publishers' keys."""
    monkeypatch.setattr(install, "KEYRING_DIRECTORY", release_keyrings)
    monkeypatch.setattr(
        install, "SIGNING_KEYS", {"terraform": release_signer.fingerprint, "tofu": release_signer.fingerprint}
    )


def engine_release(
    engine: str,
    version: str,
    binary: bytes,
    signer: ReleaseSigner,
    *,
    checksum: str | None = None,
    signature: bytes | None = None,
) -> dict[str, bytes]:
    """The archive, SHA256SUMS file and its signature one engine release serves, keyed by URL."""
    architecture = install.ARCHITECTURES[platform.machine().lower()]
    urls = install.release_urls("tofu" if engine == "tofu" else "terraform", version, architecture)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(engine, binary)
    archive = buffer.getvalue()
    digest = checksum or hashlib.sha256(archive).hexdigest()
    sums = f"{'0' * 64}  other_{version}_linux_{architecture}.zip\n{digest}  {urls.archive_name}\n".encode()
    return {
        urls.archive: archive,
        urls.sums: sums,
        urls.signature: signer.sign(sums) if signature is None else signature,
    }


def make_env(phase: str = "plan", heartbeat_interval: float | None = None) -> RunnerEnv:
    """The runner environment for one phase, beating every `heartbeat_interval` seconds when given."""
    environ = {
        "RUN_ID": RUN_ID,
        "PHASE": phase,
        "TASK_TOKEN": TASK_TOKEN,
        "API_BASE_URL": API_BASE_URL,
        "RUNNER_LOG_GROUP": LOG_GROUP,
        "AWS_REGION": "us-west-2",
    }
    if heartbeat_interval is not None:
        environ["HEARTBEAT_INTERVAL_SECONDS"] = str(heartbeat_interval)
    return RunnerEnv.from_environ(environ)


def make_clients(transport: httpx.MockTransport) -> Clients:
    """Moto backed AWS clients plus an httpx client wired to the mock transport."""
    return Clients(
        logs=boto3.client("logs", region_name="us-west-2"),
        http=httpx.Client(transport=transport),
        identity=lambda run_id: {"x-webbpulse-run-id": run_id},
    )
