"""End to end phase behaviour: plan, no changes, apply, failures and redaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import boto3
import pytest

from app.main import Clients, run
from app.models import RunnerEnvError
from tests.conftest import (
    API_BASE_URL,
    LOG_GROUP,
    PLAN_JSON_NO_CHANGES,
    RUN_ID,
    RUN_TOKEN,
    SECRET_ENVVAR,
    SECRET_TFVAR,
    TASK_TOKEN,
    WORKSPACE_ID,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
)


def log_stream_messages(stream: str) -> list[str]:
    """Every message the runner delivered to one CloudWatch Logs stream."""
    client = boto3.client("logs", region_name="us-west-2")
    streams = client.describe_log_streams(logGroupName=LOG_GROUP)["logStreams"]
    if not any(entry["logStreamName"] == stream for entry in streams):
        return []
    events = client.get_log_events(logGroupName=LOG_GROUP, logStreamName=stream, startFromHead=True)["events"]
    return [event["message"] for event in events]


def test_plan_with_changes_reports_counts(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A plan with changes exits 2, uploads both artifacts and posts the change counts."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0

    assert len(recorder.phase_results) == 1
    result = recorder.phase_results[0]
    assert result["run_id"] == RUN_ID
    assert result["phase"] == "plan"
    assert result["exit_code"] == 2
    assert result["has_changes"] is True
    assert result["changes"] == {"add": 2, "change": 1, "destroy": 2}

    assert "/runs/plan.tfplan" in recorder.uploads
    assert "/runs/plan.json" in recorder.uploads
    assert "/runs/plan.log" in recorder.uploads
    assert json.loads(recorder.uploads["/runs/plan.json"])["format_version"] == "1.2"


def test_plan_with_no_changes_reports_zero(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A plan with nothing to do exits 0 and reports no changes."""
    fake_engine(plan_exit=0, plan_json=PLAN_JSON_NO_CHANGES)
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0

    result = recorder.phase_results[0]
    assert result["exit_code"] == 0
    assert result["has_changes"] is False
    assert result["changes"] == {"add": 0, "change": 0, "destroy": 0}


def test_apply_downloads_the_plan_and_succeeds(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The apply phase fetches the saved plan and applies it."""
    fake_engine()
    recorder = ApiRecorder()
    bundle = bundle_payload(
        run_role_arn,
        plan_get_url="https://artifacts.example.invalid/runs/plan.tfplan?sig=5",
    )
    transport = make_transport(bundle, config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("apply"), clients, tmp_path) == 0

    result = recorder.phase_results[0]
    assert result["phase"] == "apply"
    assert result["exit_code"] == 0
    messages = log_stream_messages(f"{RUN_ID}/apply")
    assert any("Apply complete" in message for message in messages)


def test_apply_without_a_plan_url_fails(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """An apply bundle with no plan get url is a failure, not an unplanned apply."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("apply"), clients, tmp_path) == 1
    assert recorder.phase_results == []


def test_engine_failure_is_reported_as_failure(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A non zero plan exit that is not the detailed changes code fails the phase."""
    fake_engine(plan_exit=1)
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 1
    assert recorder.phase_results == []


def test_init_failure_stops_before_the_plan(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A failed init is reported without attempting a plan."""
    fake_engine(init_exit=3)
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 1
    assert recorder.phase_results == []


def test_bundle_fetch_failure_fails_the_task(
    aws: None,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A rejected bundle fetch fails the task and never starts the engine."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(None, config_tarball, recorder, bundle_status=403)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 1
    assert recorder.bundle_requests == 1
    assert recorder.phase_results == []
    assert log_stream_messages(f"{RUN_ID}/plan") == []


def test_tofu_engine_is_selected_from_the_bundle(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The bundle's engine field picks the binary and is named in the log."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn, engine="tofu"), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0
    messages = log_stream_messages(f"{RUN_ID}/plan")
    assert any("engine tofu" in message for message in messages)
    assert any(message.startswith("$ tofu ") for message in messages)


def test_sensitive_values_never_reach_any_log(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Variable values, tokens and credentials appear in no log sink and no upload."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0

    forbidden = [SECRET_TFVAR, SECRET_ENVVAR, RUN_TOKEN, TASK_TOKEN, WORKSPACE_ID]
    haystacks: list[str] = list(log_stream_messages(f"{RUN_ID}/plan"))
    captured = capsys.readouterr()
    haystacks.append(captured.out)
    haystacks.append(captured.err)
    haystacks.append(recorder.uploads["/runs/plan.log"].decode())

    assert any("env PROVIDER_TOKEN=" in message for message in log_stream_messages(f"{RUN_ID}/plan"))
    for secret in forbidden:
        for haystack in haystacks:
            assert secret not in haystack, f"{secret[:8]}... leaked into a log"


def test_missing_environment_is_rejected() -> None:
    """A missing runner protocol variable is a hard failure before any AWS call."""
    with pytest.raises(RunnerEnvError):
        make_env("plan").__class__.from_environ({"RUN_ID": RUN_ID})


def test_bad_phase_is_rejected() -> None:
    """Only plan and apply are phases."""
    environment: dict[str, Any] = {
        "RUN_ID": RUN_ID,
        "PHASE": "destroy",
        "TASK_TOKEN": TASK_TOKEN,
        "API_BASE_URL": API_BASE_URL,
        "RUN_TOKEN": RUN_TOKEN,
        "RUNNER_LOG_GROUP": LOG_GROUP,
    }
    with pytest.raises(RunnerEnvError):
        make_env("plan").__class__.from_environ(environment)


def test_assume_role_failure_fails_the_task(
    aws: None,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A run role that cannot be assumed fails the task without running the engine."""
    fake_engine()
    recorder = ApiRecorder()
    bundle = bundle_payload("not-an-arn")
    transport = make_transport(bundle, config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 1
    assert recorder.phase_results == []


def test_clients_build_uses_the_region(aws: None) -> None:
    """The real client factory honours the runner's region."""
    clients = Clients.build("us-west-2")
    assert clients.logs.meta.region_name == "us-west-2"
    assert clients.sts.meta.region_name == "us-west-2"
    clients.http.close()
