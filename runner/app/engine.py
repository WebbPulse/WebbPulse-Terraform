"""Running the Terraform or OpenTofu binary and streaming its combined output."""

from __future__ import annotations

import json
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import IO, Any, Mapping, Sequence, cast

from app.isolation import EngineUser, sweep
from app.logs import LogSink
from app.models import Changes, Engine, Phase

PLAN_FILE = "plan.tfplan"
PLAN_JSON_FILE = "plan.json"
NO_CHANGES_EXIT = 0
CHANGES_EXIT = 2

APPLY_SUMMARY = re.compile(
    r"^(?:Apply|Destroy) complete! Resources:"
    r"(?:\s*(?P<imported>\d+) imported,)?"
    r"(?:\s*(?P<add>\d+) added,)?"
    r"(?:\s*(?P<change>\d+) changed,)?"
    r"\s*(?P<destroy>\d+) destroyed\."
)

BASE_ENVIRONMENT = {
    "TF_IN_AUTOMATION": "1",
    "TF_INPUT": "0",
    "TF_CLI_ARGS": "-no-color",
    "CHECKPOINT_DISABLE": "1",
    "TF_PLUGIN_CACHE_DIR": "/opt/terraform-plugin-cache",
}


RUN_PHASE_VARIABLE = "TF_VAR_webbpulse_run_phase"
"""The phase a configuration can read as `var.webbpulse_run_phase` to pick a reader or writer role.

Set after the workspace's own variables so none can override it. It is only a
selector: the plan session's policy is what keeps a plan off a writer role. A
configuration declares it `ephemeral` so a saved plan does not carry `plan` into
the apply, and defaults it to `apply` so HCP Terraform, which never sets it,
keeps using the writers. An engine ignores it where it is undeclared.
"""

INTERRUPTED_EXIT = 130
"""The exit code a subcommand reports when an interrupt kept it from running."""


class EngineError(RuntimeError):
    """The engine binary is not on PATH."""


class Interrupt:
    """A stop request shared by the heartbeat, the signal handler and the engine.

    Triggering it sends SIGINT to the running subcommand, which the engine treats
    as a graceful stop that releases the state lock, and keeps any later
    subcommand from starting.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self.reason = ""

    @property
    def requested(self) -> bool:
        """Whether a stop has been asked for."""
        return bool(self.reason)

    def trigger(self, reason: str) -> None:
        """Ask the engine to stop, keeping the first reason given."""
        with self._lock:
            if not self.reason:
                self.reason = reason
            process = self._process
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)

    def attach(self, process: subprocess.Popen[str]) -> None:
        """Track the running subcommand, stopping it at once if a stop came first."""
        with self._lock:
            self._process = process
            requested = bool(self.reason)
        if requested and process.poll() is None:
            process.send_signal(signal.SIGINT)

    def detach(self) -> None:
        """Forget the subcommand once it has exited."""
        with self._lock:
            self._process = None


def resolve_binary(engine: Engine) -> str:
    """Find the engine on PATH, failing loudly rather than at the first invocation."""
    path = shutil.which(engine)
    if path is None:
        raise EngineError(f"{engine} is not on PATH")
    return path


class EngineRunner:
    """Invokes engine subcommands in one working directory with one credential set.

    With a `user` every subcommand runs as that user, and every process of the user
    is killed once the subcommand exits, before its output is read to the end.
    """

    def __init__(
        self,
        engine: Engine,
        directory: Path,
        environment: dict[str, str],
        sink: LogSink,
        binary: str | None = None,
        interrupt: Interrupt | None = None,
        user: EngineUser | None = None,
    ) -> None:
        self._binary = binary or resolve_binary(engine)
        self._interrupt = interrupt or Interrupt()
        self._engine = engine
        self._directory = directory
        self._environment = environment
        self._sink = sink
        self._user = user

    @property
    def directory(self) -> Path:
        """The working directory every subcommand runs in."""
        return self._directory

    def _identity(self) -> dict[str, Any]:
        """The Popen arguments that drop a subcommand to the engine user, empty without one."""
        if self._user is None:
            return {}
        return {"user": self._user.uid, "group": self._user.gid, "extra_groups": []}

    def _stream(self, stream: IO[str]) -> None:
        """Copy a subcommand's combined output to the sink until every writer has closed it."""
        for line in stream:
            self._sink.write(line)
        stream.close()

    def run(
        self,
        arguments: Sequence[str],
        *,
        capture: bool = False,
        extra_environment: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run one subcommand, streaming combined output to the sink line by line.

        `capture` returns stdout instead of streaming it, for the JSON producing
        subcommands whose output is a document rather than progress.
        `extra_environment` is added for this subcommand alone. Once an interrupt
        is requested no subcommand starts and each reports `INTERRUPTED_EXIT`.
        The exit code is the one the runner saw the engine exit with, and the
        engine user is swept before the output is read to its end, so nothing the
        engine left running can add to it.
        """
        if self._interrupt.requested:
            self._sink.write(f"skipped {self._engine} {' '.join(arguments)}: {self._interrupt.reason}")
            return INTERRUPTED_EXIT, ""
        command = [self._binary, *arguments]
        environment = {
            **self._environment,
            **(extra_environment or {}),
            **(self._user.environment() if self._user else {}),
        }
        self._sink.write(f"$ {self._engine} {' '.join(arguments)}")
        if capture:
            return self._run_captured(command, environment)
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=self._directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **self._identity(),
        )
        assert process.stdout is not None
        reader = threading.Thread(target=self._stream, args=(process.stdout,), daemon=True)
        reader.start()
        self._interrupt.attach(process)
        try:
            exit_code = process.wait()
        finally:
            self._interrupt.detach()
            sweep(self._user)
        reader.join()
        return exit_code, ""

    def _run_captured(self, command: list[str], environment: dict[str, str]) -> tuple[int, str]:
        """Run one subcommand into runner owned files, returning its exit code and stdout.

        The files are read only after the engine user is swept, so the document is
        what the engine printed before it exited and nothing written after.
        """
        with tempfile.TemporaryFile("w+") as stdout, tempfile.TemporaryFile("w+") as stderr:
            process = subprocess.Popen(  # noqa: S603
                command,
                cwd=self._directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                text=True,
                **self._identity(),
            )
            self._interrupt.attach(process)
            try:
                exit_code = process.wait()
            finally:
                self._interrupt.detach()
                sweep(self._user)
            stderr.seek(0)
            for line in stderr.read().splitlines():
                self._sink.write(line)
            stdout.seek(0)
            return exit_code, stdout.read()

    def init(self, extra_environment: Mapping[str, str] | None = None) -> int:
        """Initialise the working directory against the S3 backend.

        `extra_environment` carries the registry credential, which only `init`
        needs, since it is the one subcommand that downloads modules.
        """
        exit_code, _ = self.run(["init", "-input=false"], extra_environment=extra_environment)
        return exit_code

    def plan(self, *, destroy: bool = False) -> int:
        """Produce a plan file, a destroy plan when `destroy`, returning the detailed exit code."""
        arguments = ["plan", "-input=false", "-lock-timeout=120s", f"-out={PLAN_FILE}", "-detailed-exitcode"]
        if destroy:
            arguments.append("-destroy")
        exit_code, _ = self.run(arguments)
        return exit_code

    def show_plan_json(self, plan: Path | None = None) -> tuple[int, str]:
        """Render a plan file as JSON, the working directory's own unless `plan` names another."""
        return self.run(["show", "-json", str(plan) if plan else PLAN_FILE], capture=True)

    def apply(self) -> int:
        """Apply a saved plan file."""
        exit_code, _ = self.run(["apply", "-input=false", "-lock-timeout=120s", PLAN_FILE])
        return exit_code

    def output_json(self) -> tuple[int, str]:
        """Read the root module outputs as JSON, captured rather than logged."""
        return self.run(["output", "-json"], capture=True)


TASK_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "ECS_CONTAINER_METADATA_URI",
        "ECS_CONTAINER_METADATA_URI_V4",
    }
)
"""What the ECS agent gives the task to reach its role's credentials and metadata."""

VENDED_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
    }
)
"""Variables that would take the engine's AWS credentials from somewhere other than the vended profiles."""

DATA_DIRECTORY = ".terraform"
"""The engine's data directory, relative to the working directory every subcommand runs in."""

RUNNER_ONLY_KEYS: frozenset[str] = TASK_CREDENTIAL_KEYS | VENDED_CREDENTIAL_KEYS | {"TASK_TOKEN", "RUN_TOKEN"}
"""The runner's own variables, none of which the engine may inherit."""


def build_environment(
    base: dict[str, str],
    bundle_environment: dict[str, str],
    region: str,
    credential_environment: dict[str, str] | None = None,
    *,
    run_phase: Phase,
) -> dict[str, str]:
    """Assemble the engine's environment without letting the runner's own tokens through.

    `credential_environment` points the SDK at the vended run and state profiles,
    which the runner rotates in place. Static keys from anywhere would win over a
    profile and never rotate, so neither the task nor a workspace variable may set
    them, and no path to the task role's credentials survives either, though that
    role reaches nothing but the runner's log stream. `run_phase` is exported as
    `RUN_PHASE_VARIABLE`. `TF_DATA_DIR` is the relative `DATA_DIRECTORY`, so module
    paths, and every `path.module` built from them, match HCP's and stay the same
    from one run directory to the next.
    """
    blocked = TASK_CREDENTIAL_KEYS | VENDED_CREDENTIAL_KEYS
    environment = {key: value for key, value in base.items() if key not in RUNNER_ONLY_KEYS}
    environment.update(BASE_ENVIRONMENT)
    environment["AWS_REGION"] = region
    environment["AWS_DEFAULT_REGION"] = region
    environment["TF_DATA_DIR"] = DATA_DIRECTORY
    environment.update({key: value for key, value in bundle_environment.items() if key not in blocked})
    environment.update(credential_environment or {})
    environment[RUN_PHASE_VARIABLE] = run_phase
    return environment


def parse_changes(plan_json: str) -> tuple[Changes, bool]:
    """Count adds, changes and destroys from the plan JSON's resource_changes."""
    try:
        document: object = json.loads(plan_json)
    except json.JSONDecodeError:
        return Changes(), False
    if not isinstance(document, dict):
        return Changes(), False
    entries: object = cast(dict[str, object], document).get("resource_changes")
    if not isinstance(entries, list):
        return Changes(), False
    add = change = destroy = 0
    for raw_entry in cast(list[object], entries):
        if not isinstance(raw_entry, dict):
            continue
        change_block: object = cast(dict[str, object], raw_entry).get("change")
        if not isinstance(change_block, dict):
            continue
        raw_actions: object = cast(dict[str, object], change_block).get("actions")
        if not isinstance(raw_actions, list):
            continue
        actions = cast(list[object], raw_actions)
        if actions in (["no-op"], ["read"]):
            continue
        if actions == ["create"]:
            add += 1
        elif actions == ["delete"]:
            destroy += 1
        elif actions == ["update"]:
            change += 1
        elif "create" in actions and "delete" in actions:
            add += 1
            destroy += 1
    counts = Changes(add=add, change=change, destroy=destroy)
    return counts, bool(add or change or destroy)


def parse_apply_changes(lines: Sequence[str]) -> Changes | None:
    """Counts from the engine's closing `Apply complete!` or `Destroy complete!` line.

    The last matching line wins, and None means the engine printed no summary.
    """
    for line in reversed(lines):
        match = APPLY_SUMMARY.match(line.strip())
        if match is not None:
            return Changes(
                add=int(match.group("add") or 0),
                change=int(match.group("change") or 0),
                destroy=int(match.group("destroy")),
            )
    return None
