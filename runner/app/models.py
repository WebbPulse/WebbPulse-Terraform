"""Typed views of the runner's environment and of the bundle the runs domain serves."""

from __future__ import annotations

import json
import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Phase = Literal["plan", "apply"]
Engine = Literal["terraform", "tofu"]

_QUOTED_LITERAL = re.compile(r'"((?:\\[^\r\n]|[^"\\\r\n])*)"')
_HEREDOC_BODY = re.compile(r"<<-?([^\W\d][\w-]*)\r?\n(.*?)^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)


def hcl_literal_fragments(expression: str) -> list[str]:
    """The pieces of an HCL expression the engine may print on their own.

    The engine renders a list or map member by member, so the expression as typed
    rarely appears in its output. Each quoted string is registered both as typed
    and decoded, and each heredoc line on its own. This is best effort and never
    refuses a value: what it cannot recognise is still covered by the whole
    expression, which is registered beside it.
    """
    fragments = [expression]
    for match in _QUOTED_LITERAL.finditer(expression):
        raw = match.group(1)
        fragments.append(raw)
        try:
            fragments.append(json.loads(f'"{raw}"'))
        except ValueError:
            pass
    for match in _HEREDOC_BODY.finditer(expression):
        body = match.group(2)
        fragments.append(body.rstrip("\r\n"))
        fragments.extend(line.strip() for line in body.splitlines())
    return [fragment for fragment in fragments if fragment]


class RunnerEnvError(RuntimeError):
    """A required runner environment variable is missing or empty."""


class RunnerEnv(BaseModel):
    """The task definition overrides the runner is started with."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    phase: Phase
    task_token: SecretStr
    api_base_url: str
    run_token: SecretStr
    log_group: str
    region: str = "us-west-2"

    @classmethod
    def from_environ(cls, environ: dict[str, str] | None = None) -> RunnerEnv:
        """Read the runner protocol variables, raising when one is absent."""
        source = dict(os.environ if environ is None else environ)

        def required(name: str) -> str:
            value = source.get(name, "").strip()
            if not value:
                raise RunnerEnvError(f"{name} is required but was not set")
            return value

        raw_phase = required("PHASE")
        phase: Phase
        if raw_phase == "plan":
            phase = "plan"
        elif raw_phase == "apply":
            phase = "apply"
        else:
            raise RunnerEnvError(f"PHASE must be plan or apply, not {raw_phase}")
        return cls(
            run_id=required("RUN_ID"),
            phase=phase,
            task_token=SecretStr(required("TASK_TOKEN")),
            api_base_url=required("API_BASE_URL").rstrip("/"),
            run_token=SecretStr(required("RUN_TOKEN")),
            log_group=required("RUNNER_LOG_GROUP"),
            region=source.get("AWS_REGION", "").strip() or "us-west-2",
        )


class BackendConfig(BaseModel):
    """S3 backend settings for the workspace's state."""

    model_config = ConfigDict(frozen=True)

    bucket: str
    key: str
    region: str
    kms_key_id: str


class RunRole(BaseModel):
    """The per workspace role the engine runs as, plus the phase session policy."""

    model_config = ConfigDict(frozen=True)

    role_arn: str
    external_id: str
    session_policy: dict[str, object] | None = None
    session_policy_arns: list[str] = Field(default_factory=list)
    """Managed policies the session unions with the inline document, empty for
    an apply."""
    duration_seconds: int = 3600


ArtifactKind = Literal["plan", "plan_json", "log"]
"""The three objects a phase uploads, named as the artifact upload route names them."""


class Artifacts(BaseModel):
    """Presigned URLs the runner reads from.

    Uploads are not here. An upload's URL signs the exact `Content-Length` the
    runner will send, which the bundle cannot know, so each one is requested
    from `POST /runs/{id}/artifact-uploads` once the bytes exist.
    """

    model_config = ConfigDict(frozen=True)

    plan_get_url: str | None = None


class ArtifactUpload(BaseModel):
    """Where one artifact goes, and the headers its signature requires.

    Every header is inside the signature, so S3 rejects a PUT that omits or
    changes one. The runner sends them verbatim and adds nothing.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    expires_in: int = 0


class Bundle(BaseModel):
    """Everything the runs domain hands the runner for one phase."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    workspace_id: str
    engine: Engine = "terraform"
    engine_version: str | None = None
    config_url: str
    working_directory: str = ""
    """Directory within the unpacked configuration to run the engine from. Empty
    means the tarball root, which is the common case."""
    backend: BackendConfig
    run_role: RunRole
    environment_variables: dict[str, str] = Field(default_factory=dict)
    terraform_variables: dict[str, object] = Field(default_factory=dict)
    """Literal values. They go to a JSON tfvars file, where a string is a string
    whatever it contains, so quotes and braces in a value cannot be reinterpreted."""
    hcl_variables: dict[str, str] = Field(default_factory=dict)
    """Values that are HCL expressions rather than literals, which is the only way
    a list or map typed input variable can be given one. They go to a native HCL
    tfvars file, where the engine parses each one. A bundle from a control plane
    that predates the flag carries none."""
    artifacts: Artifacts = Field(default_factory=Artifacts)

    def sensitive_values(self) -> list[str]:
        """Every value that must never reach a log line."""
        values: list[str] = []
        for value in self.environment_variables.values():
            if value:
                values.append(value)
        for variable in self.terraform_variables.values():
            if isinstance(variable, str) and variable:
                values.append(variable)
        for expression in self.hcl_variables.values():
            values.extend(hcl_literal_fragments(expression))
        if self.run_role.external_id:
            values.append(self.run_role.external_id)
        return values


class Changes(BaseModel):
    """Resource counts parsed from the plan JSON."""

    model_config = ConfigDict(frozen=True)

    add: int = 0
    change: int = 0
    destroy: int = 0


class PhaseResult(BaseModel):
    """What the runner reports to the API and to Step Functions."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    phase: Phase
    exit_code: int
    changes: Changes = Field(default_factory=Changes)
    has_changes: bool = False
    error: str | None = None
    """The failure text, `None` when the phase succeeded. The API treats an
    absent and an empty error the same, so `None` is dropped rather than sent."""
