"""Typed views of the runner's environment and of the bundle the runs domain serves."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Phase = Literal["plan", "apply"]
Engine = Literal["terraform", "tofu"]


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


class Artifacts(BaseModel):
    """Presigned URLs the runner reads the plan from and writes its outputs to."""

    model_config = ConfigDict(frozen=True)

    plan_put_url: str | None = None
    plan_get_url: str | None = None
    plan_json_put_url: str | None = None
    log_put_url: str | None = None


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
