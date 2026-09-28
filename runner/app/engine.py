"""Running the Terraform or OpenTofu binary and streaming its combined output."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence, cast

from app.logs import LogSink
from app.models import Changes, Engine

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


class EngineError(RuntimeError):
    """The engine binary is not on PATH."""


def resolve_binary(engine: Engine) -> str:
    """Find the engine on PATH, failing loudly rather than at the first invocation."""
    path = shutil.which(engine)
    if path is None:
        raise EngineError(f"{engine} is not on PATH")
    return path


class EngineRunner:
    """Invokes engine subcommands in one working directory with one credential set."""

    def __init__(
        self,
        engine: Engine,
        directory: Path,
        environment: dict[str, str],
        sink: LogSink,
        binary: str | None = None,
    ) -> None:
        self._binary = binary or resolve_binary(engine)
        self._engine = engine
        self._directory = directory
        self._environment = environment
        self._sink = sink

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
        `extra_environment` is added for this subcommand alone.
        """
        command = [self._binary, *arguments]
        environment = {**self._environment, **(extra_environment or {})}
        self._sink.write(f"$ {self._engine} {' '.join(arguments)}")
        if capture:
            completed = subprocess.run(  # noqa: S603
                command,
                cwd=self._directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            for line in completed.stderr.splitlines():
                self._sink.write(line)
            return completed.returncode, completed.stdout
        process = subprocess.Popen(  # noqa: S603
            command,
            cwd=self._directory,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            self._sink.write(line)
        process.stdout.close()
        return process.wait(), ""

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

    def show_plan_json(self) -> tuple[int, str]:
        """Render the plan file as JSON."""
        return self.run(["show", "-json", PLAN_FILE], capture=True)

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

RUNNER_ONLY_KEYS: frozenset[str] = TASK_CREDENTIAL_KEYS | {
    "TASK_TOKEN",
    "RUN_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
}
"""The runner's own variables, none of which the engine may inherit."""


def build_environment(
    base: dict[str, str],
    aws_credentials: dict[str, str],
    bundle_environment: dict[str, str],
    region: str,
    directory: Path,
    backend_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Assemble the engine's environment without letting the runner's own tokens through.

    `aws_credentials` is the vended run role session, which the providers use.
    `backend_environment` points the SDK at the state profile the backend override
    names, and is applied last so a workspace variable cannot redirect where state
    credentials come from. No path to the task role's credentials survives, not
    even one a workspace variable names, though that role reaches nothing but the
    runner's log stream.
    """
    environment = {key: value for key, value in base.items() if key not in RUNNER_ONLY_KEYS}
    environment.update(BASE_ENVIRONMENT)
    environment["AWS_REGION"] = region
    environment["AWS_DEFAULT_REGION"] = region
    environment["TF_DATA_DIR"] = str(directory / ".terraform")
    environment.update({key: value for key, value in bundle_environment.items() if key not in TASK_CREDENTIAL_KEYS})
    environment.update(aws_credentials)
    environment.update(backend_environment or {})
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
