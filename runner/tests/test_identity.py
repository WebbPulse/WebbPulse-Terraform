"""The run token exchange: the signed identity proof, and that it is the only source of a token."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

import httpx
import pytest
from botocore.credentials import Credentials

from app import identity
from app.main import run
from app.models import RunnerEnv
from tests.conftest import (
    API_BASE_URL,
    LOG_GROUP,
    RUN_ID,
    RUN_TOKEN,
    TASK_TOKEN,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_transport,
)

EXCHANGED_TOKEN = "wpk_exchanged-token-do-not-log-uvwxyz"


def _env_without_token() -> RunnerEnv:
    """A runner environment started the new way, with no `RUN_TOKEN` override."""
    return RunnerEnv.from_environ(
        {
            "RUN_ID": RUN_ID,
            "PHASE": "plan",
            "TASK_TOKEN": TASK_TOKEN,
            "API_BASE_URL": API_BASE_URL,
            "RUNNER_LOG_GROUP": LOG_GROUP,
            "AWS_REGION": "us-west-2",
        }
    )


def _exchanging(inner: httpx.MockTransport, *, status: int, seen: dict[str, list[str]]) -> httpx.MockTransport:
    """Wrap a runner transport with the exchange route and record each bearer sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/runner-token"):
            seen.setdefault("exchange", []).append(json.loads(request.content)["headers"]["x-webbpulse-run-id"])
            if status != 200:
                return httpx.Response(status, json={"detail": "nope"})
            return httpx.Response(200, json={"run_token": EXCHANGED_TOKEN})
        if request.url.path.endswith(("/bundle", "/phase-result", "/artifact-uploads")):
            seen.setdefault("bearer", []).append(request.headers.get("authorization", ""))
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


class _FixedCredentials:
    """Stands in for a boto3 session's refreshable credentials."""

    def get_frozen_credentials(self) -> Credentials:
        """Fixed task role style credentials with a session token."""
        return Credentials("AKID", "SECRET", "SESSION")


def _signer(run_id: str) -> dict[str, str]:
    """A signer that produces real SigV4 headers from fixed credentials."""
    return identity.signed_identity_headers(_FixedCredentials(), "us-west-2", run_id)


def test_signed_headers_sign_the_run_id() -> None:
    """The proof names the run in a header STS verifies, so it cannot be moved to another run."""
    headers = {name.lower(): value for name, value in _signer(RUN_ID).items()}
    assert headers["x-webbpulse-run-id"] == RUN_ID
    signed = headers["authorization"].split("SignedHeaders=")[1].split(",")[0].split(";")
    assert "x-webbpulse-run-id" in signed
    assert "content-type" in signed
    assert "/us-west-2/sts/aws4_request" in headers["authorization"]
    assert headers["x-amz-security-token"] == "SESSION"


def test_no_credentials_refuses_to_sign() -> None:
    """A task with no credentials raises the error the runner falls back on."""
    with pytest.raises(identity.IdentityError):
        identity.signed_identity_headers(None, "us-west-2", RUN_ID)


def test_the_exchanged_token_is_used_for_every_runner_call(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A task started with no `RUN_TOKEN` gets one from the exchange and uses only it."""
    fake_engine()
    recorder = ApiRecorder()
    seen: dict[str, list[str]] = {}
    transport = _exchanging(
        make_transport(bundle_payload(run_role_arn), config_tarball, recorder), status=200, seen=seen
    )
    clients = replace(make_clients(transport), identity=_signer)

    assert run(_env_without_token(), clients, tmp_path) == 0
    assert seen["exchange"] == [RUN_ID]
    assert seen["bearer"] and set(seen["bearer"]) == {f"Bearer {EXCHANGED_TOKEN}"}
    assert EXCHANGED_TOKEN not in recorder.uploads["/runs/plan.log"].decode()


def test_a_refused_exchange_fails_the_task_even_with_a_run_token_override(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A `RUN_TOKEN` in the environment is ignored, so a refused exchange fails before the bundle."""
    fake_engine()
    recorder = ApiRecorder()
    seen: dict[str, list[str]] = {}
    transport = _exchanging(
        make_transport(bundle_payload(run_role_arn), config_tarball, recorder), status=401, seen=seen
    )
    clients = replace(make_clients(transport), identity=_signer)
    environment = RunnerEnv.from_environ(
        {
            "RUN_ID": RUN_ID,
            "PHASE": "plan",
            "TASK_TOKEN": TASK_TOKEN,
            "API_BASE_URL": API_BASE_URL,
            "RUNNER_LOG_GROUP": LOG_GROUP,
            "RUN_TOKEN": RUN_TOKEN,
        }
    )

    assert run(environment, clients, tmp_path) == 1
    assert recorder.bundle_requests == 0
    assert "bearer" not in seen


def test_a_task_with_no_identity_fails_before_the_bundle(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """With no signer there is nothing to exchange, and the phase fails before it asks for the bundle."""
    fake_engine()
    recorder = ApiRecorder()
    clients = replace(
        make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, recorder)), identity=None
    )

    assert run(_env_without_token(), clients, tmp_path) == 1
    assert recorder.bundle_requests == 0
    assert recorder.phase_results == []
