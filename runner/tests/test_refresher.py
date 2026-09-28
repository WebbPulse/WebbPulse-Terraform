"""Credential refresh: the API call, the refreshing thread and a phase that outlives its first session."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx
import pytest

from app.api import ApiError, CredentialsRefused, RunnerApi
from app.credential_files import RUN_PROFILE, STATE_PROFILE, CredentialFiles
from app.logs import Redactor
from app.main import run
from app.models import RefreshedCredentials, VendedCredentials
from app.refresher import CredentialRefresher
from tests.conftest import (
    PROVIDER_ACCESS_KEY_ID,
    REFRESHED_ACCESS_KEY_ID,
    REFRESHED_SECRET_ACCESS_KEY,
    REFRESHED_SESSION_TOKEN,
    REFRESHED_STATE_SECRET_ACCESS_KEY,
    REFRESHED_STATE_SESSION_TOKEN,
    RUN_TOKEN,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
    refreshed_payload,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _keys(name: str, expires_in: float) -> VendedCredentials:
    """A session named `name` expiring `expires_in` seconds after `NOW`."""
    return VendedCredentials(
        access_key_id=f"ASIA{name.upper():0<16}",
        secret_access_key=f"{name}-secret-access-key",
        session_token=f"{name}-session-token",
        expiration=(NOW + timedelta(seconds=expires_in)).isoformat(),
    )


def _api(status: int, recorder: ApiRecorder, phase: str = "apply") -> RunnerApi:
    """A client holding the run token, whose refreshes are answered with `status`."""
    transport = make_transport(None, b"", recorder, credentials_status=status)
    api = RunnerApi(make_env(phase), httpx.Client(transport=transport))
    api.exchange_token({"x-webbpulse-run-id": "run"})
    return api


def test_a_refresh_names_the_phase_and_carries_the_run_token() -> None:
    """The call is the phase's own, authenticated like every other runner call."""
    recorder = ApiRecorder()
    refreshed = _api(200, recorder).refresh_credentials()

    assert recorder.credential_requests == [{"phase": "apply"}]
    assert refreshed.aws_credentials.access_key_id == REFRESHED_ACCESS_KEY_ID
    assert set(refreshed.secrets()) == {
        REFRESHED_SECRET_ACCESS_KEY,
        REFRESHED_SESSION_TOKEN,
        REFRESHED_STATE_SECRET_ACCESS_KEY,
        REFRESHED_STATE_SESSION_TOKEN,
    }


@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_a_refusal_is_told_apart_from_an_outage(status: int) -> None:
    """A run that ended or moved on refuses, naming the code."""
    with pytest.raises(CredentialsRefused) as refusal:
        _api(status, ApiRecorder()).refresh_credentials()
    assert refusal.value.error_code == "PHASE_MISMATCH"


def test_a_server_error_is_a_passing_failure() -> None:
    """A 5xx is not a refusal, so the next check tries again."""
    with pytest.raises(ApiError) as failure:
        _api(503, ApiRecorder()).refresh_credentials()
    assert not isinstance(failure.value, CredentialsRefused)


class Harness:
    """A refresher over real files with a fixed clock and a scripted refresh."""

    def __init__(self, tmp_path: Path, outcomes: list[RefreshedCredentials | ApiError], interval: float = 60) -> None:
        self.files = CredentialFiles(tmp_path / "aws")
        self.files.write(_keys("first", 900), _keys("firststate", 900))
        self.redactor = Redactor()
        self.reports: list[str] = []
        self.calls = 0
        self.now = NOW
        self._outcomes = outcomes

        def refresh() -> RefreshedCredentials:
            self.calls += 1
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, ApiError):
                raise outcome
            return outcome

        self.refresher = CredentialRefresher(
            refresh, self.files, self.redactor, self.reports.append, interval, clock=lambda: self.now
        )


def _fresh(expires_in: float = 1800) -> RefreshedCredentials:
    return RefreshedCredentials(
        aws_credentials=_keys("second", expires_in), backend_credentials=_keys("secondstate", expires_in)
    )


def test_nothing_is_refreshed_while_the_session_has_more_than_the_lead(tmp_path: Path) -> None:
    """A session with more than ten minutes left is kept."""
    harness = Harness(tmp_path, [])
    harness.now = NOW + timedelta(seconds=299)
    assert not harness.refresher.due()
    harness.now = NOW + timedelta(seconds=300)
    assert harness.refresher.due()


def test_a_due_refresh_rotates_the_files_and_reports_only_the_expiry(tmp_path: Path) -> None:
    """The new keys reach the files and the redactor, and the report carries no key material."""
    harness = Harness(tmp_path, [_fresh()])
    assert harness.refresher.refresh_once()

    run_session = json.loads(harness.files.session_path(RUN_PROFILE).read_text())
    state_session = json.loads(harness.files.session_path(STATE_PROFILE).read_text())
    assert run_session["SecretAccessKey"] == "second-secret-access-key"
    assert state_session["SessionToken"] == "secondstate-session-token"
    assert harness.files.expires_at == NOW + timedelta(seconds=1800)
    assert harness.reports == ["credentials refreshed until 2026-09-27T12:30:00Z"]
    scrubbed = harness.redactor.scrub(json.dumps(run_session) + json.dumps(state_session))
    for secret in _fresh().secrets():
        assert secret not in scrubbed


def test_a_failed_refresh_is_retried_and_a_refusal_stops(tmp_path: Path) -> None:
    """An outage keeps the loop going; a refusal ends it, and neither touches the files."""
    harness = Harness(tmp_path, [ApiError("credential refresh returned 503"), CredentialsRefused("refused 409")])
    before = harness.files.session_path(RUN_PROFILE).read_text()

    assert harness.refresher.refresh_once() is True
    assert harness.refresher.refresh_once() is False
    assert harness.files.session_path(RUN_PROFILE).read_text() == before
    assert harness.reports == [
        "credential refresh not completed, retrying: credential refresh returned 503",
        "credential refresh refused, keeping the current keys: refused 409",
    ]


def test_the_thread_refreshes_when_due_and_stops_on_exit(tmp_path: Path) -> None:
    """Running on its own, the refresher rotates a due session once and then waits for the next expiry."""
    harness = Harness(tmp_path, [_fresh()], interval=0.01)
    harness.now = NOW + timedelta(seconds=600)
    with harness.refresher:
        deadline = time.monotonic() + 5
        while harness.refresher.refreshes < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
    assert harness.calls == 1
    assert harness.refresher.refreshes == 1


def test_a_long_phase_gets_fresh_keys_before_its_first_session_expires(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A plan outliving its bundle's session is served the refreshed keys, and none reach the log."""
    fake_engine(plan_sleep=1.0)
    recorder = ApiRecorder()
    bundle = bundle_payload(run_role_arn)
    soon = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    bundle["aws_credentials"]["expiration"] = soon
    bundle["backend"]["credentials"]["expiration"] = soon
    clients = make_clients(make_transport(bundle, config_tarball, recorder))

    assert run(make_env("plan", heartbeat_interval=0.1), clients, tmp_path) == 0

    assert recorder.credential_requests[0] == {"phase": "plan"}
    assert len(recorder.credential_requests) == 1
    run_session = json.loads((tmp_path / "aws" / f"{RUN_PROFILE}.json").read_text())
    assert run_session["AccessKeyId"] == REFRESHED_ACCESS_KEY_ID != PROVIDER_ACCESS_KEY_ID
    log = recorder.uploads["/runs/plan.log"].decode()
    assert "credentials refreshed until 2099-01-01T00:00:00Z" in log
    for secret in (*RefreshedCredentials.model_validate(refreshed_payload()).secrets(), RUN_TOKEN):
        assert secret not in log


def test_a_refused_refresh_does_not_fail_the_phase(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Refreshing is best effort: a refusal is noted and the heartbeat alone decides whether the phase stops."""
    fake_engine(plan_sleep=0.5)
    recorder = ApiRecorder()
    clients = make_clients(
        make_transport(bundle_payload(run_role_arn), config_tarball, recorder, credentials_status=409)
    )

    assert run(make_env("plan", heartbeat_interval=0.05), clients, tmp_path) == 0

    assert len(recorder.credential_requests) == 1
    assert "credential refresh refused, keeping the current keys" in recorder.uploads["/runs/plan.log"].decode()
