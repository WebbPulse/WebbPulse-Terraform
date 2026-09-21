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

RUN_ROLE_DURATION_SECONDS = 3600
"""One hour on the assumed run role, matching the plan timeout plus headroom."""


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
    """The S3 backend the runner initialises against.

    `kms_key_id` carries the key ARN. The name is the backend block's own
    argument name, so the runner writes it into the override verbatim.
    """

    bucket: str
    key: str
    region: str
    kms_key_id: str = ""


class RunRole(BaseModel):
    """The per workspace role the engine runs as, with the phase session policy.

    The external id is the workspace id, so a role trusted for one workspace
    cannot be assumed by a run against another.
    """

    role_arn: str
    external_id: str
    session_policy: dict[str, object]
    session_policy_arns: list[str] = []
    """Managed policies the session unions with the inline document. A plan
    carries `ReadOnlyAccess`, because IAM allows no wildcard in an action's
    service portion; an apply carries none."""
    duration_seconds: int = RUN_ROLE_DURATION_SECONDS


class Artifacts(BaseModel):
    """Presigned URLs the runner reads from, for one run.

    Only the read direction is minted here. An upload's URL signs the exact
    `Content-Length` the client will send, which is not known when the bundle is
    built, so the runner asks for one per artifact through
    `POST /runs/{id}/artifact-uploads` once it knows the byte count.
    """

    plan_get_url: str


ArtifactKind = Literal["plan", "plan_json", "log"]
"""The three objects a phase uploads: the binary plan, its JSON rendering and
the redacted transcript."""


class ArtifactUploadCreate(BaseModel):
    """A request for somewhere to upload one of a run's artifacts."""

    artifact: ArtifactKind
    size_bytes: int = Field(gt=0)
    """The exact byte count the presigned PUT signs, which the client has to
    declare as `Content-Length` and S3 enforces at the header."""


class ArtifactUpload(BaseModel):
    """The presigned PUT one artifact goes to.

    `headers` is not advisory: every one is inside the signature, so a request
    that omits or changes one is rejected by S3.
    """

    url: str
    headers: dict[str, str]
    expires_in: int


class RunBundle(BaseModel):
    """Everything the runner needs for one phase of one run.

    The shape is the runner's `Bundle`: the nested `backend`, `run_role` and
    `artifacts` objects are what `runner/app/models.py` validates, and the four
    extra top level fields are what the runner ignores for now but the API
    states about the phase it is serving. Uploads are not here: the runner asks
    for each one's presigned PUT by size once it has the bytes.

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
    backend: BackendConfig
    run_role: RunRole
    terraform_variables: dict[str, str]
    environment_variables: dict[str, str]
    artifacts: Artifacts


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
