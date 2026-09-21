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
    """A plan with changes uploads both artifacts and posts the change counts.

    The engine exits 2 under `-detailed-exitcode`, which is a successful plan
    with changes, so the posted exit code is 0 and `has_changes` carries it.
    """
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0

    assert len(recorder.phase_results) == 1
    result = recorder.phase_results[0]
    assert result["run_id"] == RUN_ID
    assert result["phase"] == "plan"
    assert result["exit_code"] == 0
    assert result["has_changes"] is True
    assert result["changes"] == {"add": 2, "change": 1, "destroy": 2}

    assert "/runs/plan.tfplan" in recorder.uploads
    assert "/runs/plan.json" in recorder.uploads
    assert "/runs/plan.log" in recorder.uploads
    assert json.loads(recorder.uploads["/runs/plan.json"])["format_version"] == "1.2"


def test_plan_runs_in_the_bundles_working_directory(
    aws: None,
    run_role_arn: str,
    nested_config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A working directory runs the engine there, with the override written beside it.

    The backend override and the tfvars file are only loaded from the directory
    the engine runs in, so writing them at the tarball root would silently drop
    the backend and every variable.
    """
    fake_engine()
    recorder = ApiRecorder()
    bundle = bundle_payload(run_role_arn) | {"working_directory": "infra"}
    transport = make_transport(bundle, nested_config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0
    assert recorder.phase_results[0]["exit_code"] == 0
    log = recorder.uploads["/runs/plan.log"].decode()
    assert "tfvars file zz_webbpulse.auto.tfvars.json" in log


def test_a_working_directory_the_config_lacks_fails_the_task(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A working directory that is not in the tarball fails before the engine runs."""
    fake_engine()
    recorder = ApiRecorder()
    bundle = bundle_payload(run_role_arn) | {"working_directory": "infra"}
    transport = make_transport(bundle, config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 1
    assert recorder.phase_results == []


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
    """A non zero plan exit that is not the detailed changes code fails the phase.

    Exit 1 is a real terraform failure, so it raises `PlanFailed` and posts no
    phase result, unlike the 2 that `-detailed-exitcode` uses for changes.
    """
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


def test_a_refused_artifact_upload_names_itself(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An upload the API refuses fails the phase as `ArtifactUploadFailed`.

    Before this it escaped `execute` as a bare `ApiError`, which the generic
    handler reported as the type name alone, so a run that could not store its
    plan failed with `RunnerFailed "ApiError"` and no way to tell why.
    """
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder, upload_request_status=500)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 1
    assert recorder.phase_results == []
    assert "ArtifactUploadFailed: plan upload request returned 500" in capsys.readouterr().err


def test_an_unknown_failure_keeps_its_message(
    aws: None,
    config_tarball: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exception no phase raised is reported with its message, scrubbed.

    Printing only the type name is what hid the artifact upload failure, so the
    generic handler carries the message and the redactor keeps the tokens out.
    """
    recorder = ApiRecorder()
    clients = make_clients(make_transport(None, config_tarball, recorder))

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError(f"boom with {RUN_TOKEN} in it")

    monkeypatch.setattr("app.main.execute", explode)

    assert run(make_env("plan"), clients, tmp_path) == 1
    captured = capsys.readouterr().err
    assert "RuntimeError: boom with" in captured
    assert RUN_TOKEN not in captured


def test_the_plan_artifacts_are_uploaded_at_their_real_size(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Each artifact's URL is minted for the bytes that phase actually produced."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    clients = make_clients(transport)

    assert run(make_env("plan"), clients, tmp_path) == 0

    requested = {entry["artifact"]: entry["size_bytes"] for entry in recorder.upload_requests}
    assert requested["plan"] == len(recorder.uploads["/runs/plan.tfplan"])
    assert requested["plan_json"] == len(recorder.uploads["/runs/plan.json"])
    assert requested["log"] == len(recorder.uploads["/runs/plan.log"])
    assert recorder.upload_headers["/runs/plan.tfplan"]["content-type"] == "application/octet-stream"


def test_clients_build_uses_the_region(aws: None) -> None:
    """The real client factory honours the runner's region."""
    clients = Clients.build("us-west-2")
    assert clients.logs.meta.region_name == "us-west-2"
    assert clients.sts.meta.region_name == "us-west-2"
    clients.http.close()
