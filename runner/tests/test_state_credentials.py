"""Vended credentials: the backend uses the workspace scoped state keys, the providers the run role session."""

from __future__ import annotations

import http.server
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import pytest

from app import workspace
from app.engine import RUNNER_ONLY_KEYS, TASK_CREDENTIAL_KEYS, build_environment
from app.main import run
from app.models import BackendConfig, VendedCredentials
from app.state_credentials import STATE_PROFILE, write_profile
from tests.conftest import (
    PROVIDER_ACCESS_KEY_ID,
    PROVIDER_SECRET_ACCESS_KEY,
    PROVIDER_SESSION_TOKEN,
    STATE_SECRET_ACCESS_KEY,
    STATE_SESSION_TOKEN,
    WORKSPACE_ID,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
)

KMS_KEY = "arn:aws:kms:us-west-2:870550636948:key/11111111-2222-3333-4444-555555555555"

STATE_KEYS = VendedCredentials(
    access_key_id="ASIASTATEKEY00000001",
    secret_access_key="state-secret",
    session_token="state-token",
)

TASK_CREDENTIAL_ENVIRONMENT = {
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/task",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN": "agent-token",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/var/run/secrets/token",
    "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/task",
}


def test_the_profile_holds_only_the_state_keys_in_owner_only_files(tmp_path: Path) -> None:
    """The shared credentials file names the state profile, and neither file is readable by others."""
    added = write_profile(tmp_path / "aws", STATE_KEYS)

    config = Path(added["AWS_CONFIG_FILE"])
    shared = Path(added["AWS_SHARED_CREDENTIALS_FILE"])
    assert config.read_text() == ""
    body = shared.read_text()
    assert body.startswith(f"[{STATE_PROFILE}]\n")
    assert "aws_access_key_id = ASIASTATEKEY00000001" in body
    assert "aws_secret_access_key = state-secret" in body
    assert "aws_session_token = state-token" in body
    for path in (config, shared):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "aws").stat().st_mode) == 0o700


def test_the_engine_environment_carries_no_task_credential_path() -> None:
    """Neither the task's own environment nor a workspace variable can hand the engine the task role."""
    base = {"PATH": "/usr/bin", "TASK_TOKEN": "t", "RUN_TOKEN": "r", "AWS_PROFILE": "x"} | TASK_CREDENTIAL_ENVIRONMENT
    environment = build_environment(
        base,
        VendedCredentials(
            access_key_id=PROVIDER_ACCESS_KEY_ID,
            secret_access_key=PROVIDER_SECRET_ACCESS_KEY,
            session_token=PROVIDER_SESSION_TOKEN,
        ).environment(),
        {"PROVIDER_TOKEN": "p"} | TASK_CREDENTIAL_ENVIRONMENT,
        "us-west-2",
        Path("/work"),
        {"AWS_CONFIG_FILE": "/aws/config", "AWS_SHARED_CREDENTIALS_FILE": "/aws/credentials"},
    )

    assert not TASK_CREDENTIAL_KEYS & environment.keys()
    assert not {"TASK_TOKEN", "RUN_TOKEN", "AWS_PROFILE"} & environment.keys()
    assert TASK_CREDENTIAL_KEYS <= RUNNER_ONLY_KEYS
    assert environment["AWS_ACCESS_KEY_ID"] == PROVIDER_ACCESS_KEY_ID
    assert environment["AWS_SESSION_TOKEN"] == PROVIDER_SESSION_TOKEN
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/aws/credentials"
    assert environment["PROVIDER_TOKEN"] == "p"


def test_a_phase_gives_the_engine_the_vended_keys_and_the_state_profile(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine sees the vended provider keys and the state profile, never the task endpoint or a secret."""
    for key, value in TASK_CREDENTIAL_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    fake_engine()
    recorder = ApiRecorder()
    clients = make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, recorder))

    assert run(make_env("plan"), clients, tmp_path) == 0

    log = recorder.uploads["/runs/plan.log"].decode()
    assert f"env AWS_SHARED_CREDENTIALS_FILE={tmp_path / 'aws' / 'credentials'}" in log
    assert f"env AWS_ACCESS_KEY_ID={PROVIDER_ACCESS_KEY_ID}" in log
    for key in TASK_CREDENTIAL_KEYS:
        assert key not in log
    for secret in (PROVIDER_SECRET_ACCESS_KEY, PROVIDER_SESSION_TOKEN, STATE_SECRET_ACCESS_KEY, STATE_SESSION_TOKEN):
        assert secret not in log
    shared = (tmp_path / "aws" / "credentials").read_text()
    assert STATE_SECRET_ACCESS_KEY in shared
    backend = (tmp_path / "config" / workspace.BACKEND_FILENAME).read_text()
    assert f'profile      = "{STATE_PROFILE}"' in backend
    assert f'workspace_key_prefix = "workspaces/{WORKSPACE_ID}/env"' in backend
    assert STATE_SECRET_ACCESS_KEY not in backend


@pytest.mark.skipif(shutil.which("terraform") is None, reason="needs a terraform binary")
def test_terraform_sends_state_requests_with_the_state_keys_under_the_workspace_prefix(tmp_path: Path) -> None:
    """With run role keys in the environment, a real engine signs its backend requests with the state keys.

    A loopback S3 endpoint records the access key id each request was signed with
    and the prefix of every listing, which must stay inside the workspace's own.
    """
    signed: list[str] = []
    prefixes: list[str] = []

    class S3(http.server.BaseHTTPRequestHandler):
        """Records the signing key id and answers every request as absent."""

        def handle_one(self) -> None:
            """Record and answer."""
            authorization = self.headers.get("Authorization", "")
            if "Credential=" in authorization:
                signed.append(authorization.split("Credential=", 1)[1].split("/", 1)[0])
            query = parse_qs(urlsplit(self.path).query)
            prefixes.extend(query.get("prefix", []))
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_HEAD = do_PUT = do_DELETE = do_POST = handle_one

        def log_message(self, format: str, *args: Any) -> None:
            """Stay quiet."""

    server = http.server.HTTPServer(("127.0.0.1", 0), S3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        directory = tmp_path / "config"
        directory.mkdir()
        override = workspace.write_backend_override(
            directory,
            BackendConfig(
                bucket="state",
                key="workspaces/ws/terraform.tfstate",
                region="us-west-2",
                kms_key_id=KMS_KEY,
                credentials=STATE_KEYS,
            ),
        )
        override.write_text(
            override.read_text()
            .replace(f'    kms_key_id   = "{KMS_KEY}"\n', "")
            .replace(
                "    use_lockfile = true\n",
                "    use_lockfile = true\n    use_path_style = true\n    skip_credentials_validation = true\n"
                "    skip_requesting_account_id = true\n    skip_metadata_api_check = true\n"
                f'    endpoints = {{ s3 = "http://127.0.0.1:{server.server_address[1]}" }}\n',
            )
        )
        environment = build_environment(
            {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            {"AWS_ACCESS_KEY_ID": "ASIARUNROLEKEY000001", "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t"},
            {"TF_PLUGIN_CACHE_DIR": str(tmp_path / "plugins")},
            "us-west-2",
            directory,
            write_profile(tmp_path / "aws", STATE_KEYS),
        )
        init = subprocess.run(  # noqa: S603
            [str(shutil.which("terraform")), "init", "-input=false"],
            cwd=directory,
            env=environment,
            capture_output=True,
            check=False,
        )
    finally:
        server.shutdown()
    assert signed, init.stderr
    assert set(signed) == {"ASIASTATEKEY00000001"}
    assert all(prefix.startswith("workspaces/ws/") for prefix in prefixes), prefixes
