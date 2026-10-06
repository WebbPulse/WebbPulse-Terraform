"""Entrypoint: run one phase of one run, then report to the API.

The runner holds no Step Functions permission: the API resolves the phase's task
token when a heartbeat, the result or the failure is posted, and a runner that
stops without posting is failed by the task stop consumer.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, cast

import boto3
import httpx

from app import cli_config, engine, identity, install, isolation, workspace
from app.api import ApiError, RunnerApi, build_client
from app.credential_files import CredentialFiles
from app.heartbeat import Heartbeat
from app.logs import CloudWatchLogSink, Redactor
from app.models import Bundle, Changes, PhaseResult, RunnerEnv, RunnerEnvError
from app.refresher import CredentialRefresher
from app.workload_identity import WorkloadIdentityFiles

if TYPE_CHECKING:
    from mypy_boto3_logs.client import CloudWatchLogsClient
else:
    CloudWatchLogsClient = object


RUN_ROLE_ASSUME_FAILED_CODE = "RUN_ROLE_ASSUME_FAILED"
"""The bundle's error code when the workspace's run role refused the control plane."""

WORKLOAD_IDENTITY_MISCONFIGURED_CODE = "WORKLOAD_IDENTITY_MISCONFIGURED"
"""The bundle's error code when the workspace asks for Google or Azure workload identity without its details."""


class PhaseFailure(RuntimeError):
    """A phase could not be completed; carries the short error name the API fails the task with."""

    def __init__(self, error: str, cause: str) -> None:
        super().__init__(cause)
        self.error = error
        self.cause = cause


@dataclass
class Clients:
    """The AWS and HTTP clients the runner needs, injectable for tests."""

    logs: CloudWatchLogsClient
    http: httpx.Client
    identity: Callable[[str], dict[str, str]] | None = None

    @classmethod
    def build(cls, region: str) -> Clients:
        """Real clients for the task's own execution role."""
        session = boto3.session.Session(region_name=region)
        return cls(
            logs=session.client("logs"),
            http=build_client(),
            identity=identity.session_signer(session, region),
        )


def _initial_secrets(env: RunnerEnv) -> list[str]:
    """The secrets the runner holds before it has read anything from the API."""
    return [env.task_token.get_secret_value()]


def obtain_run_token(env: RunnerEnv, clients: Clients, api: RunnerApi) -> str:
    """Get the run token by trading the task's signed identity for it.

    The exchange is the only source. A token passed on the task overrides would be
    readable by anyone who can describe the task or read the execution history, so
    none is accepted, and a failed exchange fails the phase.
    """
    if clients.identity is None:
        raise PhaseFailure("RunTokenUnavailable", "the task has no identity to exchange for a run token")
    try:
        token = api.exchange_token(clients.identity(env.run_id))
    except (ApiError, identity.IdentityError) as error:
        raise PhaseFailure("RunTokenUnavailable", str(error)) from error
    print("run token obtained from the task identity", flush=True)
    return token


def seal_plan(source: Path, destination: Path, user: isolation.EngineUser | None) -> None:
    """Copy the engine's plan file to where only the runner can write, refusing anything but that file.

    The source sits in a directory the engine user owns, so it could be swapped for
    a link to a file only the runner may read. It is opened without following a
    link and must be a regular file, owned by the engine user when there is one.
    """
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise PhaseFailure("PlanUnavailable", f"the plan file could not be opened: {type(error).__name__}") from error
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or (user is not None and status.st_uid != user.uid):
            raise PhaseFailure("PlanUnavailable", "the plan file is not a regular file the engine wrote")
        destination.write_bytes(handle.read())
    destination.chmod(0o644)


def plan_document(plan_json: str) -> str:
    """The `show -json` output, refused unless it is one JSON object.

    Anything else would otherwise count as a plan with no changes.
    """
    try:
        document: object = json.loads(plan_json)
    except json.JSONDecodeError as error:
        raise PhaseFailure("PlanShowFailed", "show -json printed no plan document") from error
    if not isinstance(document, dict):
        raise PhaseFailure("PlanShowFailed", "show -json printed no plan document")
    return plan_json


def _run_plan(
    runner: engine.EngineRunner,
    sink: CloudWatchLogSink,
    sealed_plan: Path,
    *,
    destroy: bool = False,
    init_environment: dict[str, str] | None = None,
    user: isolation.EngineUser | None = None,
) -> tuple[int, Changes, bool, str]:
    """Init, plan, seal the plan file and render it as JSON, returning the exit code and change counts.

    `destroy` plans the removal of every managed resource, which the apply phase then
    applies from the saved plan like any other.

    The plan runs under `-detailed-exitcode`, so it exits 0 with no changes and 2
    with changes. Both are successful plans, so both return 0 and the change
    counts alone say whether there were changes. Any other code is `PlanFailed`.
    `init_environment` is the registry credential and CLI config, given to `init` alone. The plan
    file is copied to `sealed_plan` once the engine user has been swept, and that
    copy is the one rendered and uploaded, so the counts and the plan the apply
    runs are the same file.
    """
    init_code = runner.init(init_environment)
    if init_code != 0:
        raise PhaseFailure("InitFailed", f"init exited {init_code}")
    plan_code = runner.plan(destroy=destroy)
    if plan_code not in (engine.NO_CHANGES_EXIT, engine.CHANGES_EXIT):
        raise PhaseFailure("PlanFailed", f"plan exited {plan_code}")
    seal_plan(runner.directory / engine.PLAN_FILE, sealed_plan, user)
    show_code, plan_json = runner.show_plan_json(sealed_plan)
    if show_code != 0:
        raise PhaseFailure("PlanShowFailed", f"show -json exited {show_code}")
    changes, has_changes = engine.parse_changes(plan_document(plan_json))
    return 0, changes, has_changes, plan_json


def _run_apply(
    runner: engine.EngineRunner,
    sink: CloudWatchLogSink,
    *,
    init_environment: dict[str, str] | None = None,
) -> tuple[int, Changes]:
    """Init and apply the saved plan, returning the counts the engine says it applied.

    `init_environment` is the registry credential and CLI config, given to `init` alone.
    """
    init_code = runner.init(init_environment)
    if init_code != 0:
        raise PhaseFailure("InitFailed", f"init exited {init_code}")
    apply_code = runner.apply()
    if apply_code != 0:
        raise PhaseFailure("ApplyFailed", f"apply exited {apply_code}")
    return apply_code, engine.parse_apply_changes(sink.lines) or Changes()


def redact_outputs(raw: str) -> str | None:
    """The `output -json` document with every sensitive value dropped, or None if unreadable.

    Only the name, type and sensitivity of a sensitive output leave the task, so the
    stored artifact never holds a value the plan itself would have masked.
    """
    try:
        document: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    redacted: dict[str, object] = {}
    for name, entry in cast(dict[str, object], document).items():
        if not isinstance(entry, dict):
            continue
        output = dict(cast(dict[str, object], entry))
        if output.get("sensitive") is True:
            output["value"] = None
        redacted[name] = output
    return json.dumps(redacted)


def _upload_outputs(runner: engine.EngineRunner, api: RunnerApi, sink: CloudWatchLogSink) -> None:
    """Best effort upload of the applied outputs; a failure is logged, never raised."""
    exit_code, raw = runner.output_json()
    document = redact_outputs(raw) if exit_code == 0 else None
    if document is None:
        sink.write("applied outputs unavailable")
        return
    try:
        api.upload_text("outputs_json", document)
    except ApiError as error:
        sink.write(f"applied outputs not uploaded: {error}")


def execute(
    env: RunnerEnv,
    clients: Clients,
    directory: Path,
    redactor: Redactor | None = None,
    api: RunnerApi | None = None,
    interrupt: engine.Interrupt | None = None,
) -> PhaseResult:
    """Fetch the bundle, run the phase, upload the artifacts and post the result.

    `redactor` and `api` are shared with the caller so a failure it reports is
    scrubbed of the run token this phase obtained and posted with that token.
    Once the token is held the phase beats its heartbeat, and a refused beat or
    `interrupt` stops the engine, failing the phase as `PhaseInterrupted`. Once the
    bundle is read the vended AWS sessions are refreshed before they expire.
    """
    redactor = redactor or Redactor(_initial_secrets(env))
    api = api or RunnerApi(env, clients.http)
    interrupt = interrupt or engine.Interrupt()
    sink = CloudWatchLogSink(clients.logs, env.log_group, f"{env.run_id}/{env.phase}", redactor)

    with sink:
        redactor.add(obtain_run_token(env, clients, api))
        refused = _refusal_handler(interrupt)
        with Heartbeat(api.heartbeat, env.heartbeat_interval_seconds, refused):
            try:
                return _run_phase(env, clients, directory, redactor, api, sink, interrupt)
            except PhaseFailure as failure:
                if interrupt.requested:
                    raise PhaseFailure("PhaseInterrupted", interrupt.reason) from failure
                raise


def _refusal_handler(interrupt: engine.Interrupt) -> Callable[[str], None]:
    """What a refused heartbeat does: stop the engine, naming the refusal."""

    def refused(detail: str) -> None:
        interrupt.trigger(f"the control plane no longer runs this phase: {detail}")

    return refused


def _run_phase(
    env: RunnerEnv,
    clients: Clients,
    directory: Path,
    redactor: Redactor,
    api: RunnerApi,
    sink: CloudWatchLogSink,
    interrupt: engine.Interrupt,
) -> PhaseResult:
    """The phase itself, from the bundle fetch to the posted result."""
    try:
        user = isolation.engine_user()
    except isolation.IsolationError as error:
        raise PhaseFailure("EngineIsolationUnavailable", str(error)) from error
    if user is not None:
        directory.chmod(0o711)
    try:
        bundle: Bundle = api.fetch_bundle()
    except ApiError as error:
        if error.error_code == RUN_ROLE_ASSUME_FAILED_CODE:
            raise PhaseFailure("AssumeRoleFailed", str(error)) from error
        if error.error_code == WORKLOAD_IDENTITY_MISCONFIGURED_CODE:
            raise PhaseFailure("WorkloadIdentityMisconfigured", str(error)) from error
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
    isolation.hand_over(config_directory, user)

    group = user.gid if user else None
    credentials = CredentialFiles(directory / "aws", group=group)
    credentials.write(bundle.aws_credentials, bundle.backend.credentials)
    identity_files = WorkloadIdentityFiles(directory / "identity", group=group)
    identity_files.write(bundle.workload_identity)
    try:
        cli_environment = cli_config.write(
            directory / "cli", bundle.registry.module_hosts if bundle.registry else {}, group
        )
    except cli_config.CliConfigError as error:
        raise PhaseFailure("CliConfigInvalid", str(error)) from error
    environment = engine.build_environment(
        dict(os.environ),
        bundle.environment_variables,
        bundle.backend.region,
        {
            **credentials.environment(),
            **identity_files.environment(),
            **(bundle.api.environment() if bundle.api else {}),
        },
        run_phase=env.phase,
    )
    refresher = CredentialRefresher(
        api.refresh_credentials,
        credentials,
        redactor,
        sink.write,
        env.heartbeat_interval_seconds,
        identity=identity_files,
    )
    with refresher:
        return _run_engine(
            env,
            clients,
            directory,
            api,
            sink,
            interrupt,
            bundle,
            engine_directory,
            environment,
            user,
            init_environment={**bundle.init_environment(), **cli_environment},
        )


def _run_engine(
    env: RunnerEnv,
    clients: Clients,
    directory: Path,
    api: RunnerApi,
    sink: CloudWatchLogSink,
    interrupt: engine.Interrupt,
    bundle: Bundle,
    engine_directory: Path,
    environment: dict[str, str],
    user: isolation.EngineUser | None = None,
    init_environment: dict[str, str] | None = None,
) -> PhaseResult:
    """Install the engine, run the phase's subcommands, upload the artifacts and post the result.

    It runs while the credential refresher keeps the engine's AWS sessions fresh.
    Every subcommand runs as `user` when there is one. The plan artifacts are kept
    in a directory only the runner can write, never in the engine's own.
    `init_environment` is the registry credential and CLI config, for `init` alone.
    """
    try:
        binary = install.ensure_engine(bundle.engine, bundle.engine_version, directory / "engines", clients.http, sink)
    except install.InstallError as error:
        raise PhaseFailure("EngineInstallFailed", str(error)) from error
    runner = engine.EngineRunner(bundle.engine, engine_directory, environment, sink, binary, interrupt, user)

    plan_path = engine_directory / engine.PLAN_FILE
    artifacts = directory / "artifacts"
    artifacts.mkdir(mode=0o755, exist_ok=True)
    sealed_plan = artifacts / engine.PLAN_FILE
    plan_json_path = artifacts / engine.PLAN_JSON_FILE
    changes = Changes()
    has_changes = False

    if env.phase == "plan":
        exit_code, changes, has_changes, plan_json = _run_plan(
            runner,
            sink,
            sealed_plan,
            destroy=bundle.is_destroy,
            init_environment=init_environment,
            user=user,
        )
        plan_json_path.write_text(plan_json)
        try:
            api.upload_file("plan", sealed_plan)
            api.upload_file("plan_json", plan_json_path)
        except ApiError as error:
            raise PhaseFailure("ArtifactUploadFailed", str(error)) from error
    else:
        if not bundle.artifacts.plan_get_url:
            raise PhaseFailure("PlanUnavailable", "the apply phase bundle carries no plan get url")
        try:
            api.download(bundle.artifacts.plan_get_url, plan_path)
        except ApiError as error:
            raise PhaseFailure("PlanDownloadFailed", str(error)) from error
        exit_code, changes = _run_apply(runner, sink, init_environment=init_environment)
        _upload_outputs(runner, api, sink)

    sink.flush()
    try:
        api.upload_text("log", sink.text())
    except ApiError as error:
        raise PhaseFailure("ArtifactUploadFailed", str(error)) from error

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


def report_failure(env: RunnerEnv, api: RunnerApi, error: str, cause: str) -> None:
    """Post a failed phase to the API, which fails the task with `error`.

    Without a run token there is nothing to post with, so the task's own stop is
    what fails the phase. A post the API refuses is left to that stop as well.
    """
    if not api.has_token:
        print("no run token, so the task stop reports this failure", file=sys.stderr, flush=True)
        return
    try:
        api.post_phase_result(
            PhaseResult(run_id=env.run_id, phase=env.phase, exit_code=1, error=cause, error_name=error)
        )
    except ApiError as failure:
        print(f"failure report not accepted: {failure}", file=sys.stderr, flush=True)


def run(env: RunnerEnv, clients: Clients, directory: Path, interrupt: engine.Interrupt | None = None) -> int:
    """Execute the phase and report a failure to the API, never raising."""
    redactor = Redactor(_initial_secrets(env))
    api = RunnerApi(env, clients.http)
    try:
        execute(env, clients, directory, redactor, api, interrupt)
    except PhaseFailure as failure:
        print(redactor.scrub(f"{failure.error}: {failure.cause}"), file=sys.stderr, flush=True)
        report_failure(env, api, failure.error, redactor.scrub(failure.cause))
        return 1
    except Exception as error:
        cause = redactor.scrub(f"{type(error).__name__}: {error}")
        print(f"unexpected failure: {cause}", file=sys.stderr, flush=True)
        report_failure(env, api, "RunnerFailed", cause)
        return 1
    return 0


def main() -> int:
    """Read the environment, build the clients and run one phase in a temporary directory."""
    try:
        env = RunnerEnv.from_environ()
    except RunnerEnvError as error:
        print(str(error), file=sys.stderr, flush=True)
        return 2
    clients = Clients.build(env.region)
    interrupt = engine.Interrupt()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: interrupt.trigger("the task was asked to stop"))
    with tempfile.TemporaryDirectory(prefix="webbpulse-run-") as temporary:
        return run(env, clients, Path(temporary), interrupt)


if __name__ == "__main__":
    raise SystemExit(main())
