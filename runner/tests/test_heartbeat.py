"""Heartbeats: the API call, the beating thread, the engine interrupt and a phase a refusal stops."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

from app.api import ApiError, HeartbeatRefused, RunnerApi
from app.engine import INTERRUPTED_EXIT, EngineRunner, Interrupt
from app.heartbeat import Heartbeat
from app.logs import LogSink
from app.main import run
from app.models import DEFAULT_HEARTBEAT_INTERVAL_SECONDS, RunnerEnvError
from tests.conftest import (
    RUN_TOKEN,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
)


class ListSink(LogSink):
    """A sink that keeps lines in memory."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        """Keep one line."""
        self.lines.append(line.rstrip("\n"))


def _api(status: int, recorder: ApiRecorder) -> RunnerApi:
    """A client holding the run token, whose heartbeats are answered with `status`."""
    transport = make_transport(None, b"", recorder, heartbeat_status=status)
    api = RunnerApi(make_env("apply"), httpx.Client(transport=transport))
    api.exchange_token({"x-webbpulse-run-id": "run"})
    return api


def test_the_default_interval_is_a_minute() -> None:
    """Without an override the runner beats once a minute."""
    assert make_env().heartbeat_interval_seconds == DEFAULT_HEARTBEAT_INTERVAL_SECONDS == 60.0
    assert make_env(heartbeat_interval=5).heartbeat_interval_seconds == 5.0


@pytest.mark.parametrize("raw", ["0", "-1", "soon"])
def test_a_bad_interval_is_rejected(raw: str) -> None:
    """An interval that is not a positive number is refused at start."""
    with pytest.raises(RunnerEnvError):
        make_env(heartbeat_interval=raw)  # type: ignore[arg-type]


def test_a_heartbeat_posts_the_phase_with_the_run_token() -> None:
    """The beat names the phase and carries the run token and nothing else."""
    recorder = ApiRecorder()
    _api(204, recorder).heartbeat()
    assert recorder.heartbeats == [{"phase": "apply"}]
    assert recorder.heartbeat_headers[0]["authorization"] == f"Bearer {RUN_TOKEN}"


@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_a_refused_heartbeat_raises_a_refusal(status: int) -> None:
    """Answers that mean the run moved on are refusals carrying the error code."""
    with pytest.raises(HeartbeatRefused) as raised:
        _api(status, ApiRecorder()).heartbeat()
    assert raised.value.error_code == "PHASE_TASK_ENDED"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_server_error_is_not_a_refusal(status: int) -> None:
    """A throttle or an outage is a passing failure, not a reason to stop."""
    with pytest.raises(ApiError) as raised:
        _api(status, ApiRecorder()).heartbeat()
    assert not isinstance(raised.value, HeartbeatRefused)


def test_the_heartbeat_keeps_beating_past_passing_failures() -> None:
    """A failed beat is retried at the next one and the refusal callback is never called."""
    answers = [ApiError("outage"), None, None, None]
    refusals: list[str] = []
    finished = threading.Event()

    def beat() -> None:
        answer = answers.pop(0) if answers else None
        if not answers:
            finished.set()
        if answer is not None:
            raise answer

    with Heartbeat(beat, 0.01, refusals.append) as heartbeat:
        assert finished.wait(5)
    assert heartbeat.beats >= 3
    assert refusals == []


def test_a_refusal_stops_the_heartbeat_and_reports_it() -> None:
    """The first refusal is reported once and ends the beat."""
    calls: list[int] = []
    refusals: list[str] = []

    def beat() -> None:
        calls.append(1)
        raise HeartbeatRefused("heartbeat refused with 409 PHASE_TASK_ENDED", "PHASE_TASK_ENDED")

    heartbeat = Heartbeat(beat, 0.01, refusals.append)
    heartbeat.start()
    deadline = time.monotonic() + 5
    while not refusals and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    heartbeat.stop()
    assert refusals == ["heartbeat refused with 409 PHASE_TASK_ENDED"]
    assert len(calls) == 1


def test_an_interrupted_engine_starts_no_subcommand(tmp_path: Path) -> None:
    """After an interrupt, every subcommand is skipped with the interrupted code."""
    interrupt = Interrupt()
    interrupt.trigger("stopped for the test")
    sink = ListSink()
    runner = EngineRunner("terraform", tmp_path, {}, sink, "/nonexistent/terraform", interrupt)
    assert runner.init() == INTERRUPTED_EXIT
    assert runner.show_plan_json() == (INTERRUPTED_EXIT, "")
    assert any("stopped for the test" in line for line in sink.lines)


def test_an_interrupt_sends_sigint_to_the_running_subcommand(tmp_path: Path) -> None:
    """A running subcommand gets SIGINT, so the engine can release its lock and exit."""
    script = tmp_path / "engine"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "print('started', flush=True)\n"
        "try:\n"
        "    time.sleep(30)\n"
        "except KeyboardInterrupt:\n"
        "    print('interrupted gracefully', flush=True)\n"
        "    sys.exit(1)\n"
    )
    script.chmod(0o755)
    interrupt = Interrupt()
    sink = ListSink()
    runner = EngineRunner("terraform", tmp_path, {}, sink, str(script), interrupt)
    timer = threading.Timer(0.5, interrupt.trigger, args=("the run ended",))
    timer.start()
    started = time.monotonic()
    code, _ = runner.run(["plan"])
    timer.cancel()
    assert code == 1
    assert time.monotonic() - started < 10
    assert "interrupted gracefully" in sink.lines


def test_a_process_attached_after_the_interrupt_is_stopped(tmp_path: Path) -> None:
    """An interrupt that lands between the check and the start still reaches the process."""
    interrupt = Interrupt()
    interrupt.trigger("already stopping")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], text=True)  # noqa: S603
    interrupt.attach(process)
    assert process.wait(timeout=10) != 0
    interrupt.detach()


def test_a_long_plan_beats_and_succeeds(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A plan that outlasts several intervals beats throughout and still posts its result."""
    fake_engine(plan_sleep=0.6)
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)

    assert run(make_env("plan", heartbeat_interval=0.1), make_clients(transport), tmp_path) == 0

    assert len(recorder.heartbeats) >= 3
    assert all(beat == {"phase": "plan"} for beat in recorder.heartbeats)
    assert recorder.failure_names() == []
    assert recorder.phase_results[0]["exit_code"] == 0


def test_a_refused_heartbeat_stops_the_plan(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """When the control plane refuses a beat, the engine is interrupted and the phase names why."""
    fake_engine(plan_sleep=30)
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder, heartbeat_status=409)

    started = time.monotonic()
    assert run(make_env("plan", heartbeat_interval=0.2), make_clients(transport), tmp_path) == 1

    assert time.monotonic() - started < 20
    assert len(recorder.heartbeats) == 1
    assert recorder.failure_names() == ["PhaseInterrupted"]
    assert "PHASE_TASK_ENDED" in recorder.phase_results[0]["error"]


def test_an_interrupt_from_the_task_stop_fails_the_phase(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The SIGTERM path: an interrupt mid plan stops the engine and is reported as the cause."""
    fake_engine(plan_sleep=30)
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)
    interrupt = Interrupt()
    timer = threading.Timer(1.0, interrupt.trigger, args=("the task was asked to stop",))
    timer.start()

    assert run(make_env("plan"), make_clients(transport), tmp_path, interrupt) == 1
    timer.cancel()

    assert recorder.failure_names() == ["PhaseInterrupted"]
    assert recorder.phase_results[0]["error"] == "the task was asked to stop"
