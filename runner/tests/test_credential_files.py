"""Vended credentials: process profiles the runner rotates, the state one for the backend, the run one for providers."""

from __future__ import annotations

import http.server
import json
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import pytest

from app import workspace
from app.credential_files import RUN_PROFILE, STATE_PROFILE, CredentialFiles, parse_expiration, process_document
from app.engine import RUNNER_ONLY_KEYS, TASK_CREDENTIAL_KEYS, VENDED_CREDENTIAL_KEYS, build_environment
from app.main import run
from app.models import BackendConfig, VendedCredentials
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
    expiration="2026-09-27T13:00:00+00:00",
)

RUN_KEYS = VendedCredentials(
    access_key_id="ASIARUNROLEKEY000001",
    secret_access_key="run-secret",
    session_token="run-token",
    expiration="2026-09-27T12:30:00.123456+00:00",
)

TASK_CREDENTIAL_ENVIRONMENT = {
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/task",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://169.254.170.23/v1/credentials",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN": "agent-token",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": "/var/run/secrets/token",
    "ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/task",
}


def test_each_profile_prints_its_own_session_in_owner_only_files(tmp_path: Path) -> None:
    """Both profiles run a process that prints their session, and nothing is readable by others."""
    files = CredentialFiles(tmp_path / "aws")
    files.write(RUN_KEYS, STATE_KEYS)

    config = files.config_path.read_text()
    for profile in (RUN_PROFILE, STATE_PROFILE):
        assert f"[profile {profile}]\ncredential_process = cat {files.session_path(profile)}\n" in config
        command = config.split(f"[profile {profile}]\ncredential_process = ", 1)[1].splitlines()[0]
        printed = json.loads(subprocess.run(command, shell=True, capture_output=True, check=True).stdout)  # noqa: S602
        expected = RUN_KEYS if profile == RUN_PROFILE else STATE_KEYS
        assert printed["Version"] == 1
        assert printed["AccessKeyId"] == expected.access_key_id
        assert printed["SecretAccessKey"] == expected.secret_access_key
        assert printed["SessionToken"] == expected.session_token
    assert "secret" not in config
    assert files.credentials_path.read_text() == ""
    for path in (
        files.config_path,
        files.credentials_path,
        *(files.session_path(p) for p in (RUN_PROFILE, STATE_PROFILE)),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "aws").stat().st_mode) == 0o700
    assert files.expires_at == parse_expiration(RUN_KEYS.expiration)
    assert files.environment() == {
        "AWS_CONFIG_FILE": str(files.config_path),
        "AWS_SHARED_CREDENTIALS_FILE": str(files.credentials_path),
        "AWS_PROFILE": RUN_PROFILE,
        "AWS_SDK_LOAD_CONFIG": "1",
    }


def test_the_sdk_is_told_a_session_expires_early() -> None:
    """The advertised expiry is the margin before the real one, in the form the SDK parses."""
    assert process_document(RUN_KEYS, 300)["Expiration"] == "2026-09-27T12:25:00Z"
    assert process_document(STATE_KEYS)["Expiration"] == "2026-09-27T12:55:00Z"
    assert "Expiration" not in process_document(RUN_KEYS.model_copy(update={"expiration": ""}))
    assert parse_expiration("soon") is None


def test_a_rotation_rewrites_the_sessions_in_place(tmp_path: Path) -> None:
    """A refresh replaces both session files without touching the profiles, and moves the next expiry on."""
    files = CredentialFiles(tmp_path / "aws")
    files.write(RUN_KEYS, STATE_KEYS)
    config = files.config_path.read_text()
    later = "2026-09-27T14:00:00+00:00"
    files.write(
        RUN_KEYS.model_copy(update={"access_key_id": "ASIAROTATED000000001", "expiration": later}),
        STATE_KEYS.model_copy(update={"access_key_id": "ASIAROTATEDSTATE0001", "expiration": later}),
    )

    assert files.config_path.read_text() == config
    assert json.loads(files.session_path(RUN_PROFILE).read_text())["AccessKeyId"] == "ASIAROTATED000000001"
    assert json.loads(files.session_path(STATE_PROFILE).read_text())["AccessKeyId"] == "ASIAROTATEDSTATE0001"
    assert files.expires_at == parse_expiration(later)
    assert sorted(path.name for path in files.directory.iterdir()) == sorted(
        ["config", "credentials", f"{RUN_PROFILE}.json", f"{STATE_PROFILE}.json"]
    )


def test_the_engine_environment_carries_no_other_credential_source() -> None:
    """Neither the task's environment nor a workspace variable can put static or task keys ahead of the profiles."""
    static = {
        "AWS_ACCESS_KEY_ID": "ASIASTATIC0000000001",
        "AWS_SECRET_ACCESS_KEY": "static-secret",
        "AWS_SESSION_TOKEN": "static-token",
        "AWS_PROFILE": "elsewhere",
    }
    base = {"PATH": "/usr/bin", "TASK_TOKEN": "t", "RUN_TOKEN": "r"} | TASK_CREDENTIAL_ENVIRONMENT | static
    environment = build_environment(
        base,
        {"PROVIDER_TOKEN": "p"} | TASK_CREDENTIAL_ENVIRONMENT | static,
        "us-west-2",
        {
            "AWS_CONFIG_FILE": "/aws/config",
            "AWS_SHARED_CREDENTIALS_FILE": "/aws/credentials",
            "AWS_PROFILE": RUN_PROFILE,
        },
        run_phase="plan",
    )

    assert not TASK_CREDENTIAL_KEYS & environment.keys()
    assert not {"TASK_TOKEN", "RUN_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & (
        environment.keys()
    )
    assert TASK_CREDENTIAL_KEYS | VENDED_CREDENTIAL_KEYS <= RUNNER_ONLY_KEYS
    assert environment["AWS_PROFILE"] == RUN_PROFILE
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
    """The engine sees the vended profiles, never static keys, the task endpoint or a secret."""
    for key, value in TASK_CREDENTIAL_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    fake_engine()
    recorder = ApiRecorder()
    clients = make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, recorder))

    assert run(make_env("plan"), clients, tmp_path) == 0

    log = recorder.uploads["/runs/plan.log"].decode()
    assert f"env AWS_CONFIG_FILE={tmp_path / 'aws' / 'config'}" in log
    assert f"env AWS_PROFILE={RUN_PROFILE}" in log
    assert "env AWS_ACCESS_KEY_ID" not in log
    for key in TASK_CREDENTIAL_KEYS:
        assert key not in log
    for secret in (PROVIDER_SECRET_ACCESS_KEY, PROVIDER_SESSION_TOKEN, STATE_SECRET_ACCESS_KEY, STATE_SESSION_TOKEN):
        assert secret not in log
    run_session = json.loads((tmp_path / "aws" / f"{RUN_PROFILE}.json").read_text())
    state_session = json.loads((tmp_path / "aws" / f"{STATE_PROFILE}.json").read_text())
    assert run_session["AccessKeyId"] == PROVIDER_ACCESS_KEY_ID
    assert state_session["SecretAccessKey"] == STATE_SECRET_ACCESS_KEY
    backend = (tmp_path / "config" / workspace.BACKEND_FILENAME).read_text()
    assert f'profile      = "{STATE_PROFILE}"' in backend
    assert f'workspace_key_prefix = "workspaces/{WORKSPACE_ID}/env"' in backend
    assert STATE_SECRET_ACCESS_KEY not in backend


@pytest.mark.skipif(shutil.which("terraform") is None, reason="needs a terraform binary")
def test_terraform_sends_state_requests_with_the_state_keys_under_the_workspace_prefix(tmp_path: Path) -> None:
    """With the run profile as the default, a real engine signs its backend requests with the state keys.

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
        files = CredentialFiles(tmp_path / "aws")
        files.write(
            RUN_KEYS.model_copy(update={"expiration": "2099-01-01T00:00:00+00:00"}),
            STATE_KEYS.model_copy(update={"expiration": "2099-01-01T00:00:00+00:00"}),
        )
        environment = build_environment(
            {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            {"TF_PLUGIN_CACHE_DIR": str(tmp_path / "plugins"), "AWS_ACCESS_KEY_ID": "ASIAWORKSPACEVAR0001"},
            "us-west-2",
            files.environment(),
            run_phase="plan",
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
