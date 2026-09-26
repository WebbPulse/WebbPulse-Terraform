"""Fixtures: a mocked AWS environment, a fake engine on PATH and an httpx mock transport."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import tarfile
import zipfile
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
    """A role the runner can assume, with the runner task role as its trusted principal."""
    iam = boto3.client("iam", region_name="us-west-2")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": WORKSPACE_ID}},
            }
        ],
    }
    role = iam.create_role(
        RoleName="WebbPulse-Terraform-staging-Terraform",
        AssumeRolePolicyDocument=json.dumps(trust),
    )
    return str(role["Role"]["Arn"])


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


BAKED_VERSION = "1.16.3"
"""The version the fake engine on PATH reports, standing in for the image's baked release."""

SECRET_OUTPUT = "sensitive-output-value-uvwxyz0123"


def bundle_payload(
    run_role_arn: str,
    *,
    engine: str = "terraform",
    plan_get_url: str | None = None,
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
        },
        "run_role": {
            "role_arn": run_role_arn,
            "external_id": WORKSPACE_ID,
            "session_policy": {
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}],
            },
        },
        "environment_variables": {"PROVIDER_TOKEN": SECRET_ENVVAR},
        "terraform_variables": {"db_password": SECRET_TFVAR, "instance_count": 2},
        "artifacts": {"plan_get_url": plan_get_url},
    }


ARTIFACT_OBJECTS: dict[str, tuple[str, str]] = {
    "plan": ("/runs/plan.tfplan", "application/octet-stream"),
    "plan_json": ("/runs/plan.json", "application/json"),
    "log": ("/runs/plan.log", "text/plain"),
    "outputs_json": ("/runs/outputs.json", "application/json"),
}
"""The path and signed content type the fake API mints an upload for, per kind."""


class ApiRecorder:
    """Records what the runner sent, so assertions can read the posted result back."""

    def __init__(self) -> None:
        self.phase_results: list[dict[str, Any]] = []
        self.uploads: dict[str, bytes] = {}
        self.upload_headers: dict[str, dict[str, str]] = {}
        self.upload_requests: list[dict[str, Any]] = []
        self.bundle_requests = 0


def make_transport(
    bundle: dict[str, Any] | None,
    config_tarball: bytes,
    recorder: ApiRecorder,
    *,
    bundle_status: int = 200,
    plan_bytes: bytes = b"fake-plan",
    upload_request_status: int = 200,
    upload_status: int = 200,
    releases: dict[str, bytes] | None = None,
    refused_uploads: frozenset[str] = frozenset(),
) -> httpx.MockTransport:
    """An httpx transport serving the bundle, the config tarball and the artifact uploads.

    The upload route mints a URL that names the declared size, and the PUT handler
    refuses a body whose length does not match it, so a runner that sent the wrong
    `Content-Length` fails here the way S3 fails it. `releases` serves engine
    release files by URL, and `refused_uploads` names artifact kinds whose upload
    request is refused.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host in ("releases.hashicorp.com", "github.com"):
            served = (releases or {}).get(str(request.url))
            return httpx.Response(404) if served is None else httpx.Response(200, content=served)
        if path.endswith("/bundle"):
            recorder.bundle_requests += 1
            if bundle_status != 200 or bundle is None:
                return httpx.Response(bundle_status, json={"detail": "nope"})
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
        echo_environment: bool = True,
        version: str = BAKED_VERSION,
        directory: Path | None = None,
    ) -> Path:
        document = json.dumps(PLAN_JSON_WITH_CHANGES if plan_json is None else plan_json)
        script = f"""#!/usr/bin/env python3
import json, os, sys

SUBCOMMAND = sys.argv[1] if len(sys.argv) > 1 else ""
ECHO = {echo_environment!r}
VERSION = {version!r}

if SUBCOMMAND == "version":
    if sys.argv[2:] == ["-json"]:
        print(json.dumps({{"terraform_version": VERSION}}))
    else:
        print("Terraform v" + VERSION)
    sys.exit(0)
if SUBCOMMAND == "init":
    print("Initializing the backend...")
    if ECHO:
        for key in sorted(os.environ):
            if key.startswith(("TF_VAR_", "PROVIDER_", "AWS_")):
                print("env " + key + "=" + os.environ[key])
        for name in sorted(os.listdir(".")):
            if name.endswith(".tfvars.json") or name.endswith(".tfvars"):
                print("tfvars file " + name)
                if name.endswith(".tfvars"):
                    for line in open(name).read().splitlines():
                        print("tfvars line " + line)
    sys.exit({init_exit})
if SUBCOMMAND == "plan":
    print("plan arguments " + " ".join(sys.argv[2:]))
    print("Terraform used the selected providers to generate the plan.")
    open("plan.tfplan", "w").write("fake-plan")
    sys.exit({plan_exit})
if SUBCOMMAND == "show":
    print(json.dumps(json.loads({document!r})))
    sys.exit(0)
if SUBCOMMAND == "apply":
    print("apply arguments " + " ".join(sys.argv[2:]))
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


def engine_release(engine: str, version: str, binary: bytes, *, checksum: str | None = None) -> dict[str, bytes]:
    """The archive and SHA256SUMS files one engine release serves, keyed by URL."""
    architecture = install.ARCHITECTURES[platform.machine().lower()]
    archive_url, sums_url, archive_name = install.release_urls(
        "tofu" if engine == "tofu" else "terraform", version, architecture
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(engine, binary)
    archive = buffer.getvalue()
    digest = checksum or hashlib.sha256(archive).hexdigest()
    sums = f"{'0' * 64}  other_{version}_linux_{architecture}.zip\n{digest}  {archive_name}\n"
    return {archive_url: archive, sums_url: sums.encode()}


def make_env(phase: str = "plan") -> RunnerEnv:
    """The runner environment for one phase."""
    return RunnerEnv.from_environ(
        {
            "RUN_ID": RUN_ID,
            "PHASE": phase,
            "TASK_TOKEN": TASK_TOKEN,
            "API_BASE_URL": API_BASE_URL,
            "RUN_TOKEN": RUN_TOKEN,
            "RUNNER_LOG_GROUP": LOG_GROUP,
            "AWS_REGION": "us-west-2",
        }
    )


def make_clients(transport: httpx.MockTransport) -> Clients:
    """Moto backed AWS clients plus an httpx client wired to the mock transport."""
    return Clients(
        logs=boto3.client("logs", region_name="us-west-2"),
        sts=boto3.client("sts", region_name="us-west-2"),
        sfn=boto3.client("stepfunctions", region_name="us-west-2"),
        http=httpx.Client(transport=transport),
    )
