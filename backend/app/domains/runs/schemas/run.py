"""Request and response models for runs, logs and the runner bundle."""

from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

ActorKind = Literal["user", "agent"]
"""How a run was triggered: `user` is a person's JWT, `agent` a `wpk_` API key
acting for the person who minted it."""


class RunActor(BaseModel):
    """Who triggered a run, snapshotted from the creating request's claims."""

    kind: ActorKind
    id: str
    """The principal's `sub`: the user for `user`, the minting user for `agent`."""
    display_name: Optional[str] = None
    """What to render, absent when the credential carried no name."""


class RunCreate(BaseModel):
    """A new run against one workspace and one config version."""

    workspace_id: str = Field(min_length=4, max_length=64)
    config_version_id: str = Field(min_length=4, max_length=64)
    plan_only: bool = False
    is_destroy: bool = False
    """Plan the destruction of every resource the workspace manages, as
    `terraform plan -destroy` does. Applying it removes them from the state."""
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
    is_destroy: bool = False
    """Whether the plan destroys every managed resource. `False` on a run created
    before destroy runs shipped, which is what those runs were."""
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
    actor: Optional[RunActor] = None
    """Who triggered this run. `None` on a run created before attribution shipped,
    since that was never recorded and cannot be recovered."""

    model_config = ConfigDict(from_attributes=True)


class RunList(BaseModel):
    """A list of runs, newest first.

    One workspace's runs in full when the request named a workspace, otherwise a
    page of every workspace's runs continued through `next_cursor`.
    """

    items: list[Run]
    next_cursor: Optional[str] = None
    """Pass back as `cursor` to read the next page. `None` on the last page and
    always on a single workspace's list."""


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


PlanAction = Literal["create", "update", "delete", "replace", "read", "no-op"]
"""One resource or output's change, flattened from Terraform's action list.

Terraform reports a replacement as the two element list `["delete", "create"]`
or `["create", "delete"]`, depending on whether the provider replaces before or
after destroying. Both collapse to `replace` here, because the ordering is a
provider detail the viewer has no use for.
"""

PlanMode = Literal["managed", "data"]
"""Whether a change is to a managed resource or to a data source read."""


class PlanResourceChange(BaseModel):
    """One resource's entry in a plan, as the viewer renders it.

    `before` and `after` are the planned state on either side, with every value
    the plan marked sensitive already replaced by a redaction string, so no
    sealed variable or sensitive attribute reaches the browser. They carry
    whatever shape the plan put there rather than an object, because the plan
    format allows a scalar or a list and a run view that 500s on an unusual
    plan is a worse failure than a loose type.
    """

    address: str
    module_address: str = ""
    mode: PlanMode
    type: str
    name: str
    provider_name: str = ""
    action: PlanAction
    action_reason: str = ""
    before: Any = None
    after: Any = None
    after_unknown: Optional[dict[str, Any]] = None
    replace_paths: list[list[Union[str, int]]] = Field(default_factory=list)
    """The attribute paths that forced a replacement, each a list of steps."""
    before_sensitive: Optional[Union[dict[str, Any], bool]] = None
    after_sensitive: Optional[Union[dict[str, Any], bool]] = None
    """Terraform's own sensitivity map, kept so the viewer can mark a field even
    where the value itself was redacted away."""


class PlanOutputChange(BaseModel):
    """One root output's change in a plan.

    A sensitive output carries the redaction string rather than its value, the
    same way a sensitive resource attribute does.
    """

    name: str
    action: PlanAction
    before: Any = None
    after: Any = None
    after_unknown: bool = False
    sensitive: bool = False


class RunPlan(BaseModel):
    """A run's plan as structured data, derived from `terraform show -json`.

    The raw plan document is never returned: it can reach hundreds of megabytes
    and it carries sensitive values verbatim. This is the summarised, redacted
    projection the run view renders.
    """

    run_id: str
    terraform_version: str = ""
    changes: RunChanges
    resource_changes: list[PlanResourceChange] = Field(default_factory=list)
    output_changes: list[PlanOutputChange] = Field(default_factory=list)
    has_changes: bool = False


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
    `artifacts` objects are what `runner/app/models.py` validates, as is
    `is_destroy`, which selects `plan -destroy`. The other extra top level fields
    are what the runner ignores for now but the API states about the phase it is
    serving. Uploads are not here: the runner asks
    for each one's presigned PUT by size once it has the bytes.

    The only response in the API that carries decrypted variable values, which is
    why it is gated on a run token bound to this run rather than on scopes.
    """

    run_id: str
    workspace_id: str
    phase: Phase
    plan_only: bool
    is_destroy: bool = False
    """Whether the plan phase runs `plan -destroy`. The apply phase applies the
    saved plan either way."""
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
    """What the runner reports when a phase ends.

    Extra top level fields the runner sends, such as `run_id` and `has_changes`,
    are ignored: the id comes from the path and the change flag from `changes`.
    """

    phase: Phase
    exit_code: int
    changes: RunChanges = RunChanges()
    error: Optional[str] = Field(default="", max_length=4096)
    """The failure text, empty when the phase succeeded. Absent, null and empty
    all mean the same thing and all normalise to the empty string, so the
    service always reads a str."""

    @field_validator("error", mode="before")
    @classmethod
    def _error_is_never_null(cls, value: object) -> object:
        """Turn a null error into the empty string the service expects."""
        return "" if value is None else value


class PhaseResultAccepted(BaseModel):
    """The run as it stands after a phase result was recorded."""

    run_id: str
    status: RunStatus
