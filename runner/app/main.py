"""Entrypoint: run one phase of one run, then report to the API and Step Functions."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import httpx

from app import callback, credentials, engine, workspace
from app.api import ApiError, RunnerApi, build_client
from app.logs import CloudWatchLogSink, Redactor
from app.models import Bundle, Changes, PhaseResult, RunnerEnv, RunnerEnvError

if TYPE_CHECKING:
    from mypy_boto3_logs.client import CloudWatchLogsClient
    from mypy_boto3_stepfunctions.client import SFNClient
    from mypy_boto3_sts.client import STSClient
else:
    CloudWatchLogsClient = SFNClient = STSClient = object


class PhaseFailure(RuntimeError):
    """A phase could not be completed; carries the short error name for the callback."""

    def __init__(self, error: str, cause: str) -> None:
        super().__init__(cause)
        self.error = error
        self.cause = cause


@dataclass
class Clients:
    """The AWS and HTTP clients the runner needs, injectable for tests."""

    logs: CloudWatchLogsClient
    sts: STSClient
    sfn: SFNClient
    http: httpx.Client

    @classmethod
    def build(cls, region: str) -> Clients:
        """Real clients for the task's own execution role."""
        session = boto3.session.Session(region_name=region)
        return cls(
            logs=session.client("logs"),
            sts=session.client("sts"),
            sfn=session.client("stepfunctions"),
            http=build_client(),
        )


def _run_plan(runner: engine.EngineRunner, sink: CloudWatchLogSink) -> tuple[int, Changes, bool, str]:
    """Init, plan and render the plan JSON, returning the exit code and change counts."""
    init_code = runner.init()
    if init_code != 0:
        raise PhaseFailure("InitFailed", f"init exited {init_code}")
    plan_code = runner.plan()
    if plan_code not in (engine.NO_CHANGES_EXIT, engine.CHANGES_EXIT):
        raise PhaseFailure("PlanFailed", f"plan exited {plan_code}")
    show_code, plan_json = runner.show_plan_json()
    if show_code != 0:
        raise PhaseFailure("PlanShowFailed", f"show -json exited {show_code}")
    changes, has_changes = engine.parse_changes(plan_json)
    sink.write(f"Plan: {changes.add} to add, {changes.change} to change, {changes.destroy} to destroy.")
    return plan_code, changes, has_changes, plan_json


def _run_apply(runner: engine.EngineRunner) -> int:
    """Init and apply the saved plan."""
    init_code = runner.init()
    if init_code != 0:
        raise PhaseFailure("InitFailed", f"init exited {init_code}")
    apply_code = runner.apply()
    if apply_code != 0:
        raise PhaseFailure("ApplyFailed", f"apply exited {apply_code}")
    return apply_code


def execute(env: RunnerEnv, clients: Clients, directory: Path) -> PhaseResult:
    """Fetch the bundle, run the phase, upload the artifacts and post the result."""
    redactor = Redactor([env.run_token.get_secret_value(), env.task_token.get_secret_value()])
    api = RunnerApi(env, clients.http)
    sink = CloudWatchLogSink(clients.logs, env.log_group, f"{env.run_id}/{env.phase}", redactor)

    with sink:
        try:
            bundle: Bundle = api.fetch_bundle()
        except ApiError as error:
            raise PhaseFailure("BundleFetchFailed", str(error)) from error
        redactor.extend(bundle.sensitive_values())

        sink.write(f"run {bundle.run_id} workspace {bundle.workspace_id} phase {env.phase} engine {bundle.engine}")

        config_directory = directory / "config"
        archive = directory / "config.tar.gz"
        try:
            api.download(bundle.config_url, archive)
        except ApiError as error:
            raise PhaseFailure("ConfigDownloadFailed", str(error)) from error
        try:
            engine_directory = workspace.prepare(config_directory, bundle, archive)
        except workspace.ConfigError as error:
            raise PhaseFailure("ConfigUnpackFailed", str(error)) from error

        try:
            aws_credentials = credentials.assume_run_role(clients.sts, bundle.run_role, env.run_id, env.phase)
        except credentials.CredentialsError as error:
            raise PhaseFailure("AssumeRoleFailed", str(error)) from error
        redactor.extend(aws_credentials.values())

        environment = engine.build_environment(
            dict(os.environ),
            aws_credentials,
            bundle.environment_variables,
            bundle.backend.region,
            engine_directory,
        )

        try:
            runner = engine.EngineRunner(bundle.engine, engine_directory, environment, sink)
        except engine.EngineError as error:
            raise PhaseFailure("EngineMissing", str(error)) from error

        plan_path = engine_directory / engine.PLAN_FILE
        plan_json_path = engine_directory / engine.PLAN_JSON_FILE
        changes = Changes()
        has_changes = False

        if env.phase == "plan":
            exit_code, changes, has_changes, plan_json = _run_plan(runner, sink)
            plan_json_path.write_text(plan_json)
            api.upload_file(bundle.artifacts.plan_put_url, plan_path, "application/octet-stream")
            api.upload_file(bundle.artifacts.plan_json_put_url, plan_json_path, "application/json")
        else:
            if not bundle.artifacts.plan_get_url:
                raise PhaseFailure("PlanUnavailable", "the apply phase bundle carries no plan get url")
            try:
                api.download(bundle.artifacts.plan_get_url, plan_path)
            except ApiError as error:
                raise PhaseFailure("PlanDownloadFailed", str(error)) from error
            exit_code = _run_apply(runner)

        sink.flush()
        api.upload_text(bundle.artifacts.log_put_url, sink.text(), "text/plain")

        result = PhaseResult(
            run_id=env.run_id,
            phase=env.phase,
            exit_code=exit_code,
            changes=changes,
            has_changes=has_changes,
        )
        try:
            api.post_phase_result(result)
        except ApiError as error:
            raise PhaseFailure("PhaseResultPostFailed", str(error)) from error
        return result


def run(env: RunnerEnv, clients: Clients, directory: Path) -> int:
    """Execute the phase and send task success or task failure, never raising."""
    redactor = Redactor([env.run_token.get_secret_value(), env.task_token.get_secret_value()])
    try:
        result = execute(env, clients, directory)
    except PhaseFailure as failure:
        print(redactor.scrub(f"{failure.error}: {failure.cause}"), file=sys.stderr, flush=True)
        callback.send_failure(
            clients.sfn,
            env.task_token.get_secret_value(),
            failure.error,
            redactor.scrub(failure.cause),
        )
        return 1
    except Exception as error:
        print(f"unexpected failure: {type(error).__name__}", file=sys.stderr, flush=True)
        callback.send_failure(
            clients.sfn,
            env.task_token.get_secret_value(),
            "RunnerFailed",
            type(error).__name__,
        )
        return 1
    callback.send_success(clients.sfn, env.task_token.get_secret_value(), result.exit_code, result.changes)
    return 0


def main() -> int:
    """Read the environment, build the clients and run one phase in a temporary directory."""
    try:
        env = RunnerEnv.from_environ()
    except RunnerEnvError as error:
        print(str(error), file=sys.stderr, flush=True)
        return 2
    clients = Clients.build(env.region)
    with tempfile.TemporaryDirectory(prefix="webbpulse-run-") as temporary:
        return run(env, clients, Path(temporary))


if __name__ == "__main__":
    raise SystemExit(main())
