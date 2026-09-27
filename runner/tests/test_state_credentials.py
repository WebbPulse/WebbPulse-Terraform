"""The state profile: the backend reads and writes state as the task, the providers act as the run role."""

from __future__ import annotations

import http.server
import json
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from app import state_credentials, workspace
from app.engine import build_environment
from app.main import run
from app.models import BackendConfig
from app.state_credentials import STATE_PROFILE, StateCredentialsError, resolve, write_profile
from tests.conftest import (
    WORKSPACE_ID,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
)

KMS_KEY = "arn:aws:kms:us-west-2:870550636948:key/11111111-2222-3333-4444-555555555555"

TASK_RESPONSE = {
    "AccessKeyId": "ASIATASKROLEKEY00001",
    "SecretAccessKey": "task-secret",
    "Token": "task-session-token",
    "Expiration": "2099-01-01T00:00:00Z",
}


class FakeFetcher:
    """Records what the process asked the container endpoint for."""

    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response or TASK_RESPONSE
        self.error = error
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def retrieve_uri(self, relative_uri: str) -> dict[str, Any]:
        """Answer a relative path request."""
        self.calls.append((relative_uri, None))
        if self.error:
            raise self.error
        return self.response

    def retrieve_full_uri(self, full_url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Answer a full URL request."""
        self.calls.append((full_url, headers))
        if self.error:
            raise self.error
        return self.response


def mode(path: Path) -> int:
    """The permission bits of a path."""
    return stat.S_IMODE(path.stat().st_mode)


def test_backend_override_names_the_state_profile(tmp_path: Path) -> None:
    """The backend is pointed at the state profile and never at a credential value."""
    backend = BackendConfig(bucket="b", key="k", region="us-west-2", kms_key_id="kms")
    body = workspace.write_backend_override(tmp_path, backend).read_text()
    assert f'profile      = "{STATE_PROFILE}"' in body
    assert "access_key" not in body
    assert "token" not in body


def test_profile_carries_only_the_container_endpoint(tmp_path: Path) -> None:
    """The source file holds the endpoint and nothing else from the runner's environment."""
    environ = {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc",
        "RUN_TOKEN": "wpk_do_not_copy",
        "AWS_ACCESS_KEY_ID": "task-key",
    }
    added = write_profile(tmp_path / "aws", environ, "/opt/venv/bin/python")
    source = tmp_path / "aws" / "container.json"
    assert json.loads(source.read_text()) == {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"}
    config = Path(added["AWS_CONFIG_FILE"]).read_text()
    assert f"[profile {STATE_PROFILE}]" in config
    assert (
        f"credential_process = /opt/venv/bin/python -I {Path(state_credentials.__file__).resolve()} {source}" in config
    )
    assert Path(added["AWS_SHARED_CREDENTIALS_FILE"]).read_text() == ""
    assert mode(tmp_path / "aws") == 0o700
    for path in (source, Path(added["AWS_CONFIG_FILE"]), Path(added["AWS_SHARED_CREDENTIALS_FILE"])):
        assert mode(path) == 0o600


def test_backend_environment_wins_over_workspace_variables(tmp_path: Path) -> None:
    """A workspace env var cannot point the backend at another config file, and the run role stays for providers."""
    environment = build_environment(
        {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"},
        {"AWS_ACCESS_KEY_ID": "run-role-key", "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t"},
        {"AWS_CONFIG_FILE": "/tmp/elsewhere", "AWS_SHARED_CREDENTIALS_FILE": "/tmp/elsewhere-too"},
        "us-west-2",
        tmp_path,
        {"AWS_CONFIG_FILE": "/work/aws/config", "AWS_SHARED_CREDENTIALS_FILE": "/work/aws/credentials"},
    )
    assert environment["AWS_CONFIG_FILE"] == "/work/aws/config"
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/work/aws/credentials"
    assert environment["AWS_ACCESS_KEY_ID"] == "run-role-key"
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in environment


def test_resolve_uses_the_relative_uri() -> None:
    """On Fargate the task role comes from the relative URI, shaped as a version 1 result."""
    fetcher = FakeFetcher()
    result = resolve({"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"}, fetcher)
    assert fetcher.calls == [("/v2/credentials/abc", None)]
    assert result == {
        "Version": 1,
        "AccessKeyId": "ASIATASKROLEKEY00001",
        "SecretAccessKey": "task-secret",
        "SessionToken": "task-session-token",
        "Expiration": "2099-01-01T00:00:00Z",
    }


def test_resolve_sends_the_authorization_token_for_a_full_uri(tmp_path: Path) -> None:
    """A full URI gets its authorization token, from the file when one is named."""
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    fetcher = FakeFetcher()
    resolve(
        {
            "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://127.0.0.1:51679/creds",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN": "env-token",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(token_file),
        },
        fetcher,
    )
    assert fetcher.calls == [("http://127.0.0.1:51679/creds", {"Authorization": "file-token"})]


def test_resolve_without_an_endpoint_fails() -> None:
    """Outside a task there is nothing to fetch, and the process says so."""
    with pytest.raises(StateCredentialsError, match="no container credential endpoint"):
        resolve({}, FakeFetcher())


def test_resolve_wraps_a_fetch_failure_without_its_message() -> None:
    """A fetch failure names only its type, so no URL or token reaches the engine's log."""
    fetcher = FakeFetcher(error=RuntimeError("secret detail /v2/credentials/abc"))
    with pytest.raises(StateCredentialsError) as caught:
        resolve({"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/abc"}, fetcher)
    assert "secret detail" not in str(caught.value)


@pytest.fixture
def credential_endpoint() -> Iterator[tuple[str, list[str]]]:
    """A loopback endpoint answering like the ECS agent, recording the Authorization headers."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        """Serves the task response."""

        def do_GET(self) -> None:
            """Answer one credential request."""
            seen.append(self.headers.get("Authorization", ""))
            body = json.dumps(TASK_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Stay quiet."""

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/creds", seen
    finally:
        server.shutdown()


def test_the_profile_command_prints_the_task_credentials(
    tmp_path: Path, credential_endpoint: tuple[str, list[str]]
) -> None:
    """The exact command the profile names runs isolated and prints what the SDK expects."""
    url, seen = credential_endpoint
    added = write_profile(
        tmp_path / "aws",
        {"AWS_CONTAINER_CREDENTIALS_FULL_URI": url, "AWS_CONTAINER_AUTHORIZATION_TOKEN": "agent-token"},
        sys.executable,
    )
    line = next(
        entry for entry in Path(added["AWS_CONFIG_FILE"]).read_text().splitlines() if "credential_process" in entry
    )
    command = line.split(" = ", 1)[1].split(" ")
    completed = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, check=False, cwd=tmp_path, env={"PYTHONPATH": "/nonexistent"}
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["AccessKeyId"] == "ASIATASKROLEKEY00001"
    assert seen == ["agent-token"]


def test_the_profile_command_fails_cleanly_outside_a_task(tmp_path: Path) -> None:
    """With no endpoint the command exits 1 with a short reason and prints no credentials."""
    added = write_profile(tmp_path / "aws", {}, sys.executable)
    line = next(
        entry for entry in Path(added["AWS_CONFIG_FILE"]).read_text().splitlines() if "credential_process" in entry
    )
    completed = subprocess.run(  # noqa: S603
        line.split(" = ", 1)[1].split(" "), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "no container credential endpoint" in completed.stderr


def test_a_phase_gives_the_engine_the_run_role_and_the_state_profile(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine sees the assumed run role as keys and the state profile's config file, never the endpoint."""
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/credentials/task")
    fake_engine()
    recorder = ApiRecorder()
    clients = make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, recorder))

    assert run(make_env("plan"), clients, tmp_path) == 0

    log = recorder.uploads["/runs/plan.log"].decode()
    assert f"env AWS_CONFIG_FILE={tmp_path / 'aws' / 'config'}" in log
    assert "env AWS_ACCESS_KEY_ID=" in log
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in log
    source = json.loads((tmp_path / "aws" / "container.json").read_text())
    assert source == {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/task"}
    backend = (tmp_path / "config" / workspace.BACKEND_FILENAME).read_text()
    assert f'profile      = "{STATE_PROFILE}"' in backend
    assert WORKSPACE_ID in backend


@pytest.mark.skipif(shutil.which("terraform") is None, reason="needs a terraform binary")
def test_terraform_sends_state_requests_with_the_profile_not_the_environment(
    tmp_path: Path, credential_endpoint: tuple[str, list[str]]
) -> None:
    """With run role keys in the environment, a real engine signs its backend requests as the task.

    A loopback S3 endpoint records the access key id each request was signed with.
    """
    url, _ = credential_endpoint
    signed: list[str] = []

    class S3(http.server.BaseHTTPRequestHandler):
        """Records the signing key id and answers every request as absent."""

        def handle_one(self) -> None:
            """Record and answer."""
            authorization = self.headers.get("Authorization", "")
            if "Credential=" in authorization:
                signed.append(authorization.split("Credential=", 1)[1].split("/", 1)[0])
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
                bucket="state", key="workspaces/ws/terraform.tfstate", region="us-west-2", kms_key_id=KMS_KEY
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
        backend_environment = write_profile(
            tmp_path / "aws", {"AWS_CONTAINER_CREDENTIALS_FULL_URI": url}, sys.executable
        )
        environment = build_environment(
            {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            {"AWS_ACCESS_KEY_ID": "ASIARUNROLEKEY000001", "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t"},
            {"TF_PLUGIN_CACHE_DIR": str(tmp_path / "plugins")},
            "us-west-2",
            directory,
            backend_environment,
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
    assert set(signed) == {"ASIATASKROLEKEY00001"}
