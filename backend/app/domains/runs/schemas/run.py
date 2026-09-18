"""Request and response models for runs, logs and the runner bundle."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

RunStatus = Literal[
    "pending",
    "planning",
    "planned",
    "awaiting_confirmation",
    "applying",
    "applied",
    "planned_and_finished",
    "errored",
    "cancelled",
    "discarded",
]
"""Every state a run can hold, as the contract fixes them."""

Phase = Literal["plan", "apply"]
"""Which half of a run a task, a log stream or a phase result belongs to."""


class RunCreate(BaseModel):
    """A new run against one workspace and one config version."""

    workspace_id: str = Field(min_length=4, max_length=64)
    config_version_id: str = Field(min_length=4, max_length=64)
    plan_only: bool = False
    message: str = Field(default="", max_length=1024)


class RunChanges(BaseModel):
    """What a plan found: resources to add, change and destroy."""

    add: int = 0
    change: int = 0
    destroy: int = 0


class Run(BaseModel):
    """A stored run as the API renders it.

    The task tokens and the run token hash are stored on the row and never in
    this model: a caller who could read a confirm task token could confirm a run
    it was not allowed to confirm.
    """

    run_id: str
    workspace_id: str
    config_version_id: str
    status: RunStatus
    plan_only: bool
    message: str = ""
    created_at: str
    updated_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    queued_behind: Optional[str] = None
    """The run this one waits on, when it was queued rather than started."""
    changes: Optional[RunChanges] = None
    error: Optional[str] = None
    execution_arn: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RunList(BaseModel):
    """One workspace's runs, newest first."""

    items: list[Run]


class RunCreated(Run):
    """A newly created run. Carries the run token only when the run started.

    A queued run has no execution and so no token: the token is minted when the
    state machine starts, which is when the run ahead of it finishes.
    """

    run_token: Optional[str] = None


class LogEvent(BaseModel):
    """One line from the runner's log stream."""

    timestamp: int
    message: str


class LogPage(BaseModel):
    """A page of one run phase's logs, and the token that continues it."""

    run_id: str
    phase: Phase
    events: list[LogEvent]
    next_after: Optional[str] = None
    """Pass back as `after` to read what arrives next. `None` when the stream
    does not exist yet, which is not the same as an empty page."""


class BackendConfig(BaseModel):
    """The S3 backend the runner initialises against."""

    bucket: str
    key: str
    region: str
    kms_key_arn: str = ""
    use_lockfile: bool = True
    """Terraform 1.11 native S3 locking. The runner floor is >= 1.11 for this."""


class PlanArtifacts(BaseModel):
    """Presigned URLs for the plan artifacts of one run.

    A plan phase PUTs the two files and an apply phase GETs the binary plan, so
    both directions are signed and the runner needs no bucket credentials.
    """

    plan_put_url: str
    plan_json_put_url: str
    plan_get_url: str


class RunBundle(BaseModel):
    """Everything the runner needs for one phase of one run.

    The only response in the API that carries decrypted variable values, which is
    why it is gated on a run token bound to this run rather than on scopes.
    """

    run_id: str
    workspace_id: str
    phase: Phase
    plan_only: bool
    engine: Literal["terraform", "tofu"]
    engine_version: str
    working_directory: str
    config_url: str
    backend_config: BackendConfig
    run_role_arn: str
    session_policy: dict[str, object]
    terraform_variables: dict[str, str]
    env_variables: dict[str, str]
    plan_artifacts: PlanArtifacts


class PhaseResult(BaseModel):
    """What the runner reports when a phase ends."""

    phase: Phase
    exit_code: int
    changes: RunChanges = RunChanges()
    error: str = Field(default="", max_length=4096)


class PhaseResultAccepted(BaseModel):
    """The run as it stands after a phase result was recorded."""

    run_id: str
    status: RunStatus
