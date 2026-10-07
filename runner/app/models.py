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


DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
"""How often the runner tells the control plane it is alive, well inside the state's heartbeat timeout."""


def _interval(raw: str) -> float:
    """The heartbeat interval a variable names, the default when unset, refused when not positive."""
    if not raw.strip():
        return DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise RunnerEnvError(f"HEARTBEAT_INTERVAL_SECONDS must be a number, not {raw}") from error
    if value <= 0:
        raise RunnerEnvError("HEARTBEAT_INTERVAL_SECONDS must be positive")
    return value


class RunnerEnv(BaseModel):
    """The task definition overrides the runner is started with."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    phase: Phase
    task_token: SecretStr
    api_base_url: str
    log_group: str
    region: str = "us-west-2"
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS

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
            log_group=required("RUNNER_LOG_GROUP"),
            region=source.get("AWS_REGION", "").strip() or "us-west-2",
            heartbeat_interval_seconds=_interval(source.get("HEARTBEAT_INTERVAL_SECONDS", "")),
        )


class VendedCredentials(BaseModel):
    """One session's keys, vended by the control plane for this phase alone.

    The runner assumes no role. Its task role reaches nothing but its log stream,
    so whatever the engine runs cannot reach a workspace role through the task's
    credential endpoint; the keys arrive in the bundle instead.
    """

    model_config = ConfigDict(frozen=True)

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str = ""

    def secrets(self) -> list[str]:
        """The parts that must never reach a log line."""
        return [value for value in (self.secret_access_key, self.session_token) if value]


class GcpWorkloadIdentity(BaseModel):
    """A phase's Google identity token and what the Google provider needs to exchange it."""

    model_config = ConfigDict(frozen=True)

    token: str
    expiration: str = ""
    audience: str
    """The workload identity provider as Google STS names it, `//iam.googleapis.com/<provider>`."""
    service_account_email: str = ""
    """The service account to impersonate; empty uses the federated identity directly."""


class AzureWorkloadIdentity(BaseModel):
    """A phase's Azure identity token and the client id it federates with."""

    model_config = ConfigDict(frozen=True)

    token: str
    expiration: str = ""
    client_id: str


class WorkloadIdentity(BaseModel):
    """The identity tokens the workspace's `TFC_GCP_PROVIDER_AUTH` and `TFC_AZURE_PROVIDER_AUTH` ask for."""

    model_config = ConfigDict(frozen=True)

    gcp: GcpWorkloadIdentity | None = None
    azure: AzureWorkloadIdentity | None = None

    def secrets(self) -> list[str]:
        """The tokens, which must never reach a log line."""
        return [entry.token for entry in (self.gcp, self.azure) if entry and entry.token]


class RefreshedCredentials(BaseModel):
    """A fresh pair of sessions for the running phase, from `POST /runs/{id}/credentials`."""

    model_config = ConfigDict(frozen=True)

    aws_credentials: VendedCredentials
    backend_credentials: VendedCredentials
    workload_identity: WorkloadIdentity | None = None
    """Fresh Google and Azure identity tokens, absent unless the workspace asks for them."""

    def secrets(self) -> list[str]:
        """Both sessions' parts and any identity token, none of which may reach a log line."""
        identity = self.workload_identity.secrets() if self.workload_identity else []
        return [*self.aws_credentials.secrets(), *self.backend_credentials.secrets(), *identity]


def token_variable(host: str) -> str:
    """The `TF_TOKEN_<host>` name the engine reads a host's credential from.

    Dots become underscores and hyphens double underscores, the encoding
    Terraform and OpenTofu both use for hosts in variable names.
    """
    return "TF_TOKEN_" + host.replace("-", "__").replace(".", "_")


class RegistryCredentials(BaseModel):
    """The run's short lived, read only module registry credential.

    It reaches the engine only for `init`, the one subcommand that installs
    modules, so the plan and apply never hold it.
    """

    model_config = ConfigDict(frozen=True)

    hosts: list[str] = Field(default_factory=lambda: list[str]())
    token: str
    expires_at: str = ""

    def environment(self) -> dict[str, str]:
        """One `TF_TOKEN_<host>` per registry host."""
        return {token_variable(host): self.token for host in self.hosts if host}


class ApiCredentials(BaseModel):
    """The run's own short lived key on this control plane, for the WebbPulse provider.

    Only a workspace an admin opted in gets one, and it reaches the engine for every
    subcommand as `WEBBPULSE_TF_TOKEN`, beside the API origin and the access gate's
    header value, after workspace variables so they cannot override it.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    token: str
    expires_at: str = ""
    origin_verify: str | None = None

    def environment(self) -> dict[str, str]:
        """The provider's host, token and, behind the access gate, its header value."""
        values = {"WEBBPULSE_TF_HOST": self.host, "WEBBPULSE_TF_TOKEN": self.token}
        if self.origin_verify:
            values["WEBBPULSE_TF_ORIGIN_VERIFY"] = self.origin_verify
        return values

    def secrets(self) -> list[str]:
        """The values the redactor must hide."""
        return [value for value in (self.token, self.origin_verify) if value]


class BackendConfig(BaseModel):
    """S3 backend settings for the workspace's state, with keys scoped to its prefix."""

    model_config = ConfigDict(frozen=True)

    bucket: str
    key: str
    region: str
    kms_key_id: str
    credentials: VendedCredentials


ArtifactKind = Literal["plan", "plan_json", "log", "outputs_json", "workdir"]
"""The objects a phase uploads, named as the artifact upload route names them."""


class Artifacts(BaseModel):
    """Presigned URLs the runner reads from.

    Uploads are not here. An upload's URL signs the exact `Content-Length` the
    runner will send, which the bundle cannot know, so each one is requested
    from `POST /runs/{id}/artifact-uploads` once the bytes exist.
    """

    model_config = ConfigDict(frozen=True)

    plan_get_url: str | None = None
    workdir_get_url: str | None = None
    """The planned working directory, in an apply phase bundle whose plan archived one."""


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
    run_role_arn: str = ""
    """The workspace run role the provider keys are a session of, for the transcript."""
    aws_credentials: VendedCredentials
    """The workspace run role's keys for this phase, read only for a plan."""
    plan_only: bool = False
    """The run ends after its plan, so the plan archives no working directory for an apply."""
    is_destroy: bool = False
    """Plan the destruction of every managed resource with `plan -destroy`. Only the
    plan phase reads it: the apply applies the saved plan, which already carries the
    destroy mode. A bundle from a control plane that predates destroy runs has none."""
    target_addrs: list[str] = Field(default_factory=lambda: list[str]())
    """Resource addresses the plan is limited to, each passed as `-target`. Only the plan
    phase reads them, since the saved plan already carries the targeting."""
    replace_addrs: list[str] = Field(default_factory=lambda: list[str]())
    """Resource addresses the plan must replace, each passed as `-replace`."""
    refresh: bool = True
    """False plans with `-refresh=false`, skipping the read of remote objects."""
    refresh_only: bool = False
    """Plan with `-refresh-only`, proposing only to update state to match remote objects."""
    run_variables: dict[str, str] = Field(default_factory=dict)
    """Variables set for this run alone, as `terraform plan -var` sends them through a cloud
    block: each value is an HCL expression. They go to a native tfvars file passed with
    `-var-file`, which the engine reads after every `.auto.tfvars` file, so they win over
    workspace variables and the configuration's own files as HCP's run variables do."""
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
    registry: RegistryCredentials | None = None
    """The private registry credential for `init`. A bundle from a control plane with
    no registry host, or one that predates it, carries none."""

    workload_identity: WorkloadIdentity | None = None
    """Google and Azure identity tokens for this phase, absent unless the workspace asks for them."""
    api: ApiCredentials | None = None
    """The run's control plane API token, absent unless the workspace grants its runs scopes."""

    def init_environment(self) -> dict[str, str]:
        """The variables `init` alone adds to the engine's environment."""
        return self.registry.environment() if self.registry else {}

    def sensitive_values(self) -> list[str]:
        """Every value that must never reach a log line."""
        values: list[str] = []
        for value in self.environment_variables.values():
            if value:
                values.append(value)
        for variable in self.terraform_variables.values():
            if isinstance(variable, str) and variable:
                values.append(variable)
        for expression in [*self.hcl_variables.values(), *self.run_variables.values()]:
            values.extend(hcl_literal_fragments(expression))
        values.extend(self.aws_credentials.secrets())
        values.extend(self.backend.credentials.secrets())
        if self.registry and self.registry.token:
            values.append(self.registry.token)
        if self.workload_identity:
            values.extend(self.workload_identity.secrets())
        if self.api:
            values.extend(self.api.secrets())
        return values


class Changes(BaseModel):
    """Resource counts, from the plan JSON for a plan and the engine summary for an apply."""

    model_config = ConfigDict(frozen=True)

    add: int = 0
    change: int = 0
    destroy: int = 0


class PhaseResult(BaseModel):
    """What the runner reports to the API, which resolves the phase's task token."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    phase: Phase
    exit_code: int
    changes: Changes = Field(default_factory=Changes)
    has_changes: bool = False
    error: str | None = None
    """The failure text, `None` when the phase succeeded. The API treats an
    absent and an empty error the same, so `None` is dropped rather than sent."""
    error_name: str | None = None
    """The short error name of a phase that failed before it had a result. The
    API fails the phase's task with it, so the run's error names it."""
