"""Running the Terraform or OpenTofu binary and streaming its combined output."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from app.logs import LogSink
from app.models import Changes, Engine

PLAN_FILE = "plan.tfplan"
PLAN_JSON_FILE = "plan.json"
NO_CHANGES_EXIT = 0
CHANGES_EXIT = 2

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
    ) -> None:
        self._binary = resolve_binary(engine)
        self._engine = engine
        self._directory = directory
        self._environment = environment
        self._sink = sink

    def run(self, arguments: Sequence[str], *, capture: bool = False) -> tuple[int, str]:
        """Run one subcommand, streaming combined output to the sink line by line.

        `capture` returns stdout instead of streaming it, for the JSON producing
        subcommands whose output is a document rather than progress.
        """
        command = [self._binary, *arguments]
        self._sink.write(f"$ {self._engine} {' '.join(arguments)}")
        if capture:
            completed = subprocess.run(  # noqa: S603
                command,
                cwd=self._directory,
                env=self._environment,
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
            env=self._environment,
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

    def init(self) -> int:
        """Initialise the working directory against the S3 backend."""
        exit_code, _ = self.run(["init", "-input=false"])
        return exit_code

    def plan(self) -> int:
        """Produce a plan file, returning the detailed exit code."""
        exit_code, _ = self.run(
            ["plan", "-input=false", "-lock-timeout=120s", f"-out={PLAN_FILE}", "-detailed-exitcode"]
        )
        return exit_code

    def show_plan_json(self) -> tuple[int, str]:
        """Render the plan file as JSON."""
        return self.run(["show", "-json", PLAN_FILE], capture=True)

    def apply(self) -> int:
        """Apply a saved plan file."""
        exit_code, _ = self.run(["apply", "-input=false", "-lock-timeout=120s", PLAN_FILE])
        return exit_code


def build_environment(
    base: dict[str, str],
    aws_credentials: dict[str, str],
    bundle_environment: dict[str, str],
    region: str,
    directory: Path,
) -> dict[str, str]:
    """Assemble the engine's environment without letting the runner's own tokens through."""
    environment = {
        key: value
        for key, value in base.items()
        if key
        not in {
            "TASK_TOKEN",
            "RUN_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        }
    }
    environment.update(BASE_ENVIRONMENT)
    environment["AWS_REGION"] = region
    environment["AWS_DEFAULT_REGION"] = region
    environment["TF_DATA_DIR"] = str(directory / ".terraform")
    environment.update(bundle_environment)
    environment.update(aws_credentials)
    return environment


def parse_changes(plan_json: str) -> tuple[Changes, bool]:
    """Count adds, changes and destroys from the plan JSON's resource_changes."""
    try:
        document = json.loads(plan_json)
    except json.JSONDecodeError:
        return Changes(), False
    if not isinstance(document, dict):
        return Changes(), False
    add = change = destroy = 0
    resource_changes = document.get("resource_changes")
    if not isinstance(resource_changes, list):
        return Changes(), False
    for entry in resource_changes:
        if not isinstance(entry, dict):
            continue
        change_block = entry.get("change")
        if not isinstance(change_block, dict):
            continue
        actions = change_block.get("actions")
        if not isinstance(actions, list):
            continue
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
