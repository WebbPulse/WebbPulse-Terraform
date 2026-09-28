"""Google and Azure workload identity: token files the providers read, rotated on refresh, never logged."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from app.credential_files import CredentialFiles
from app.logs import REDACTED, Redactor
from app.main import run
from app.models import GcpWorkloadIdentity, RefreshedCredentials, WorkloadIdentity
from app.refresher import CredentialRefresher
from app.workload_identity import GOOGLE_STS_URL, JWT_TOKEN_TYPE, WorkloadIdentityFiles, external_account
from tests.conftest import (
    AZURE_CLIENT_ID,
    AZURE_IDENTITY_TOKEN,
    GCP_AUDIENCE,
    GCP_IDENTITY_TOKEN,
    GCP_SERVICE_ACCOUNT,
    RUN_ID,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
    refreshed_payload,
    workload_identity_payload,
)
from tests.test_runner import log_stream_messages

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _identity(**overrides: Any) -> WorkloadIdentity:
    """Both clouds' tokens, as a workspace asking for both is given them."""
    return WorkloadIdentity.model_validate(workload_identity_payload(**overrides))


def test_the_google_credential_impersonates_the_service_account(tmp_path: Path) -> None:
    """The `external_account` reads the token file and trades it at STS for the service account."""
    document = external_account(_identity().gcp or pytest.fail("no gcp"), tmp_path / "token")

    assert document["type"] == "external_account"
    assert document["audience"] == GCP_AUDIENCE
    assert document["subject_token_type"] == JWT_TOKEN_TYPE
    assert document["token_url"] == GOOGLE_STS_URL
    assert document["credential_source"] == {"file": str(tmp_path / "token")}
    assert document["service_account_impersonation_url"] == (
        f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{GCP_SERVICE_ACCOUNT}:generateAccessToken"
    )


def test_without_a_service_account_the_federated_identity_is_used_directly(tmp_path: Path) -> None:
    """Google's direct resource access: no impersonation URL."""
    identity = GcpWorkloadIdentity(token="t", audience=GCP_AUDIENCE)
    assert "service_account_impersonation_url" not in external_account(identity, tmp_path / "token")


def test_the_tokens_are_owner_only_files_the_environment_points_at(tmp_path: Path) -> None:
    """Neither token is in the environment itself; each is a file only the runner's user can read."""
    files = WorkloadIdentityFiles(tmp_path / "identity")
    files.write(_identity())
    environment = files.environment()

    assert environment == {
        "GOOGLE_APPLICATION_CREDENTIALS": str(files.gcp_credentials_path),
        "ARM_USE_OIDC": "true",
        "ARM_OIDC_TOKEN_FILE_PATH": str(files.azure_token_path),
        "ARM_CLIENT_ID": AZURE_CLIENT_ID,
    }
    assert GCP_IDENTITY_TOKEN not in json.dumps(environment)
    assert AZURE_IDENTITY_TOKEN not in json.dumps(environment)
    assert files.gcp_token_path.read_text() == GCP_IDENTITY_TOKEN
    assert files.azure_token_path.read_text() == AZURE_IDENTITY_TOKEN
    credential = json.loads(files.gcp_credentials_path.read_text())
    assert credential["credential_source"]["file"] == str(files.gcp_token_path)
    assert stat.S_IMODE(files.directory.stat().st_mode) == 0o700
    for path in (files.gcp_token_path, files.gcp_credentials_path, files.azure_token_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_phase_without_tokens_gets_nothing(tmp_path: Path) -> None:
    """No files, no variables, no expiry to refresh for."""
    files = WorkloadIdentityFiles(tmp_path / "identity")
    files.write(None)
    files.write(WorkloadIdentity())

    assert files.environment() == {}
    assert files.expires_at is None
    assert not files.directory.exists()


def test_only_the_asked_for_cloud_is_configured(tmp_path: Path) -> None:
    """An Azure only workspace gets no Google credential."""
    files = WorkloadIdentityFiles(tmp_path / "identity")
    files.write(WorkloadIdentity.model_validate({"azure": workload_identity_payload()["azure"]}))

    assert "GOOGLE_APPLICATION_CREDENTIALS" not in files.environment()
    assert files.environment()["ARM_USE_OIDC"] == "true"


def test_a_refresh_rewrites_the_token_files_in_place(tmp_path: Path) -> None:
    """The providers keep the paths they were started with and read the new token from them."""
    files = WorkloadIdentityFiles(tmp_path / "identity")
    files.write(_identity())
    before = files.environment()
    files.write(_identity(gcp_token="new.gcp.token", azure_token="new.azure.token"))

    assert files.environment() == before
    assert files.gcp_token_path.read_text() == "new.gcp.token"
    assert files.azure_token_path.read_text() == "new.azure.token"


def test_the_refresher_rotates_the_tokens_and_redacts_them_first(tmp_path: Path) -> None:
    """A refresh registers the new tokens with the redactor, then rewrites the files."""
    credentials = CredentialFiles(tmp_path / "aws")
    credentials.write(
        RefreshedCredentials.model_validate(refreshed_payload()).aws_credentials,
        RefreshedCredentials.model_validate(refreshed_payload()).backend_credentials,
    )
    identity = WorkloadIdentityFiles(tmp_path / "identity")
    identity.write(_identity(expiration=(NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    refreshed = RefreshedCredentials.model_validate(
        {**refreshed_payload(), "workload_identity": workload_identity_payload(gcp_token="fresh.gcp.jwt")}
    )
    redactor = Redactor()
    reports: list[str] = []
    refresher = CredentialRefresher(
        lambda: refreshed, credentials, redactor, reports.append, 60, clock=lambda: NOW, identity=identity
    )

    assert refresher.due(), "an identity token near its expiry makes a refresh due"
    assert refresher.refresh_once()
    assert identity.gcp_token_path.read_text() == "fresh.gcp.jwt"
    assert redactor.scrub("token fresh.gcp.jwt") == f"token {REDACTED}"
    assert not refresher.due()
    assert all("fresh.gcp.jwt" not in report for report in reports)


def test_the_engine_reads_the_tokens_and_no_log_holds_them(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The providers are pointed at the files, and a token the engine prints is redacted."""
    fake_engine()
    recorder = ApiRecorder()
    bundle = {
        **bundle_payload(run_role_arn),
        "environment_variables": {"ARM_CLIENT_ID": "workspace-set-client"},
        "workload_identity": workload_identity_payload(),
    }
    transport = make_transport(bundle, config_tarball, recorder)

    assert run(make_env("plan"), make_clients(transport), tmp_path) == 0

    messages = log_stream_messages(f"{RUN_ID}/plan")
    assert any(message.startswith("env GOOGLE_APPLICATION_CREDENTIALS=") for message in messages)
    assert "env ARM_USE_OIDC=true" in messages
    assert f"env ARM_CLIENT_ID={AZURE_CLIENT_ID}" in messages
    assert any(message.startswith("env ARM_OIDC_TOKEN_FILE_PATH=") for message in messages)
    assert messages.count(f"token file {REDACTED}") == 2
    captured = capsys.readouterr()
    for haystack in [*messages, captured.out, captured.err, recorder.uploads["/runs/plan.log"].decode()]:
        assert GCP_IDENTITY_TOKEN not in haystack
        assert AZURE_IDENTITY_TOKEN not in haystack


def test_a_misconfigured_workspace_fails_the_phase_by_name(
    aws: None,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A flag without its provider or client id is the workspace's mistake, named as such."""
    fake_engine()
    recorder = ApiRecorder()
    envelope = {"message": "TFC_GCP_PROVIDER_AUTH is true but", "error_code": "WORKLOAD_IDENTITY_MISCONFIGURED"}
    transport = make_transport(None, config_tarball, recorder, bundle_status=409, bundle_body=envelope)

    assert run(make_env("plan"), make_clients(transport), tmp_path) == 1
    assert recorder.failure_names() == ["WorkloadIdentityMisconfigured"]
