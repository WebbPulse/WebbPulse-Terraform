"""Fixtures: a mocked AWS environment, a fake engine on PATH and an httpx mock transport."""

from __future__ import annotations

import io
import json
import os
import tarfile
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


def bundle_payload(run_role_arn: str, *, engine: str = "terraform", plan_get_url: str | None = None) -> dict[str, Any]:
    """A bundle the runs domain would serve, carrying sensitive variable values."""
    return {
        "run_id": RUN_ID,
        "workspace_id": WORKSPACE_ID,
        "engine": engine,
        "engine_version": "1.16.3",
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
        "artifacts": {
            "plan_put_url": "https://artifacts.example.invalid/runs/plan.tfplan?sig=2",
            "plan_get_url": plan_get_url,
            "plan_json_put_url": "https://artifacts.example.invalid/runs/plan.json?sig=3",
            "log_put_url": "https://artifacts.example.invalid/runs/plan.log?sig=4",
        },
    }


class ApiRecorder:
    """Records what the runner sent, so assertions can read the posted result back."""

    def __init__(self) -> None:
        self.phase_results: list[dict[str, Any]] = []
        self.uploads: dict[str, bytes] = {}
        self.bundle_requests = 0


def make_transport(
    bundle: dict[str, Any] | None,
    config_tarball: bytes,
    recorder: ApiRecorder,
    *,
    bundle_status: int = 200,
    plan_bytes: bytes = b"fake-plan",
) -> httpx.MockTransport:
    """An httpx transport serving the bundle, the config tarball and the presigned puts."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/bundle"):
            recorder.bundle_requests += 1
            if bundle_status != 200 or bundle is None:
                return httpx.Response(bundle_status, json={"detail": "nope"})
            return httpx.Response(200, json=bundle)
        if path.endswith("/phase-result"):
            recorder.phase_results.append(json.loads(request.content))
            return httpx.Response(204)
        if "config.tar.gz" in path:
            return httpx.Response(200, content=config_tarball)
        if request.method == "PUT":
            recorder.uploads[path] = request.content
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
    ) -> Path:
        document = json.dumps(PLAN_JSON_WITH_CHANGES if plan_json is None else plan_json)
        script = f"""#!/usr/bin/env python3
import json, os, sys

SUBCOMMAND = sys.argv[1] if len(sys.argv) > 1 else ""
ECHO = {echo_environment!r}

if SUBCOMMAND == "version":
    print("Terraform v0.0.0-fake")
    sys.exit(0)
if SUBCOMMAND == "init":
    print("Initializing the backend...")
    if ECHO:
        for key in sorted(os.environ):
            if key.startswith(("TF_VAR_", "PROVIDER_", "AWS_")):
                print("env " + key + "=" + os.environ[key])
        for name in sorted(os.listdir(".")):
            if name.endswith(".tfvars.json"):
                print("tfvars file " + name)
    sys.exit({init_exit})
if SUBCOMMAND == "plan":
    print("Terraform used the selected providers to generate the plan.")
    open("plan.tfplan", "w").write("fake-plan")
    sys.exit({plan_exit})
if SUBCOMMAND == "show":
    print(json.dumps(json.loads({document!r})))
    sys.exit(0)
if SUBCOMMAND == "apply":
    print("Apply complete! Resources: 1 added, 0 changed, 0 destroyed.")
    sys.exit({apply_exit})
print("unexpected subcommand " + SUBCOMMAND, file=sys.stderr)
sys.exit(1)
"""
        for name in ("terraform", "tofu"):
            target = bin_directory / name
            target.write_text(script)
            target.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
        return bin_directory

    return install


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
