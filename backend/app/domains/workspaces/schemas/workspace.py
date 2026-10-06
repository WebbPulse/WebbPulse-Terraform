"""Request and response models for workspaces, variables and config versions."""

from __future__ import annotations

import posixpath
import re
from datetime import datetime
from typing import Any, Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Engine = Literal["terraform", "tofu"]
"""Which binary runs this workspace. The runner image bundles both."""

VariableCategory = Literal["terraform", "env"]
"""A `terraform` variable becomes a `-var` on the command line; an `env` variable
becomes a process environment variable on the task."""


VCS_REPO_PATTERN: Final = r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$"
"""A GitHub `owner/name`."""

WORKING_DIRECTORY_MAX_LENGTH: Final = 255
TRIGGER_PATTERN_MAX_LENGTH: Final = 255
_BRANCH_FORBIDDEN: Final = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")

PLAN_ASSUME_ROLE_ARN_PATTERN: Final = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")
"""An exact IAM role ARN, the same shape `external_run_role_arns` accepts in Terraform. No wildcards."""

PLAN_ASSUME_ROLE_ARNS_MAX: Final = 10
"""How many reader roles one workspace may name."""

PLAN_ASSUME_ROLE_ARN_MAX_LENGTH: Final = 140
"""The longest reader role ARN accepted.

With the count cap this keeps the plan's inline session policy, its secret read
statements and reader roles together, plus the `ReadOnlyAccess` ARN beside it,
inside the 2,048 plaintext characters STS allows for session policies in total.
It still leaves a 64 character role name a 45 character path.
"""


def normalize_working_directory(value: str) -> str:
    """A working directory as a clean relative path, `""` for the repository root.

    Leading `./`, repeated and trailing slashes are dropped. An absolute path, a `..`
    segment, a backslash or a control character is refused, since the runner resolves
    the directory inside the archive and would refuse it later anyway.
    """
    candidate = value.strip()
    if not candidate or candidate == ".":
        return ""
    if len(candidate) > WORKING_DIRECTORY_MAX_LENGTH:
        raise ValueError(f"working_directory is longer than {WORKING_DIRECTORY_MAX_LENGTH} characters")
    if candidate.startswith("/"):
        raise ValueError("working_directory must be relative to the repository root")
    if "\\" in candidate or any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ValueError("working_directory must be a plain relative path")
    if ".." in candidate.split("/"):
        raise ValueError("working_directory cannot leave the repository with '..'")
    normalized = posixpath.normpath(candidate)
    return "" if normalized == "." else normalized


def validate_branch(value: str) -> str:
    """A branch name git would accept, following `git check-ref-format --branch`."""
    candidate = value.strip()
    if (
        not candidate
        or _BRANCH_FORBIDDEN.search(candidate)
        or candidate.startswith(("/", "-", "."))
        or candidate.endswith(("/", ".", ".lock"))
        or "/." in candidate
        or candidate == "@"
    ):
        raise ValueError("tracked_branch is not a valid branch name")
    return candidate


def validate_trigger_patterns(values: list[str]) -> list[str]:
    """Trimmed, non-empty patterns, each within the length limit, duplicates dropped."""
    cleaned: list[str] = []
    for value in values:
        pattern = value.strip()
        if not pattern:
            raise ValueError("a trigger pattern cannot be empty")
        if len(pattern) > TRIGGER_PATTERN_MAX_LENGTH:
            raise ValueError(f"a trigger pattern is longer than {TRIGGER_PATTERN_MAX_LENGTH} characters")
        if pattern not in cleaned:
            cleaned.append(pattern)
    return cleaned


def validate_plan_role_arn(value: str) -> str:
    """A trimmed exact role ARN for the plan role, refusing wildcards and oversized values."""
    arn = value.strip()
    if len(arn) > PLAN_ASSUME_ROLE_ARN_MAX_LENGTH or not PLAN_ASSUME_ROLE_ARN_PATTERN.match(arn):
        raise ValueError(
            "plan_role_arn must be an exact IAM role ARN of the form "
            "arn:aws:iam::<12 digit account id>:role/<name>, with no wildcards"
        )
    return arn


def validate_plan_assume_role_arns(values: list[str]) -> list[str]:
    """Trimmed exact role ARNs, refusing wildcards, oversized entries and repeats.

    These are the only roles a plan session may assume, so a pattern would widen
    what a plan can reach past what the person named, and a repeat usually means a
    typo in the other entry.
    """
    cleaned: list[str] = []
    for value in values:
        arn = value.strip()
        if len(arn) > PLAN_ASSUME_ROLE_ARN_MAX_LENGTH:
            raise ValueError(f"a plan assume role ARN is longer than {PLAN_ASSUME_ROLE_ARN_MAX_LENGTH} characters")
        if not PLAN_ASSUME_ROLE_ARN_PATTERN.match(arn):
            raise ValueError(
                "plan_assume_role_arns entries must be exact IAM role ARNs of the form "
                "arn:aws:iam::<12 digit account id>:role/<name>, with no wildcards"
            )
        if arn in cleaned:
            raise ValueError(f"plan_assume_role_arns repeats {arn}")
        cleaned.append(arn)
    return cleaned


class RunRoleSetup(BaseModel):
    """What a person needs to build the run role for one workspace.

    The role cannot exist before the workspace does: its trust policy names the
    workspace id as the external id, so the id has to be handed out first. Every
    workspace response carries these three values so the setup can be followed
    without reading the stack's outputs. The control plane's credential vending
    role is the only principal the trust policy may name: it assumes the role on
    each phase's behalf, so no runner task holds a path to it.
    """

    principal_arn: str
    """The credential vending role the run role's trust policy has to name."""
    principal_arns: list[str] = []
    """Every principal the trust policy has to name, which is the vending role alone."""
    external_id: str
    """The workspace id, which the runner sends as `sts:ExternalId`."""
    role_name: str
    """The name the role has to carry to fall inside the runner's AssumeRole grant."""


class WorkspaceBase(BaseModel):
    """Fields every workspace representation carries."""

    engine: Engine = "terraform"
    engine_version: str = Field(min_length=1, max_length=32)
    run_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
    plan_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
    """A read only role plans assume instead of the run role. Applies keep the run role."""
    working_directory: str = ""
    description: str = ""
    vcs_repo: Optional[str] = Field(default=None, max_length=140, pattern=VCS_REPO_PATTERN)
    """The GitHub repository, as `owner/name`, whose uploads may start runs here."""
    tracked_branch: Optional[str] = Field(default=None, min_length=1, max_length=255)
    """The branch whose pushes start a normal run. No branch means pushes are ignored."""
    trigger_patterns: list[str] = Field(default_factory=list, max_length=50)
    """Glob patterns over repository paths. An upload starts a run only when a changed
    path matches one. Empty means everything under the working directory."""
    speculative_plans: bool = True
    """Whether a pull request upload starts a plan only run."""
    file_triggers_enabled: bool = True
    """Whether uploads are filtered by changed paths. False always triggers a run, the
    way HCP Terraform's "Always trigger runs" does."""
    plan_assume_role_arns: list[str] = Field(default_factory=list, max_length=PLAN_ASSUME_ROLE_ARNS_MAX)
    """Exact role ARNs a plan session may assume beside its read only access, such as
    Route 53 reader roles. An apply is not limited by this list."""


class WorkspaceCreate(WorkspaceBase):
    """A new workspace. The name is unique across the environment."""

    name: str = Field(min_length=1, max_length=90, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    @field_validator("working_directory")
    @classmethod
    def _normalize_working_directory(cls, value: str) -> str:
        """Store the directory as a clean relative path."""
        return normalize_working_directory(value)

    @field_validator("tracked_branch")
    @classmethod
    def _validate_branch(cls, value: Optional[str]) -> Optional[str]:
        """Refuse a name git would not accept as a branch."""
        return None if value is None else validate_branch(value)

    @field_validator("trigger_patterns")
    @classmethod
    def _validate_trigger_patterns(cls, value: list[str]) -> list[str]:
        """Trim the patterns and refuse empty or oversized ones."""
        return validate_trigger_patterns(value)

    @field_validator("plan_assume_role_arns")
    @classmethod
    def _validate_plan_assume_role_arns(cls, value: list[str]) -> list[str]:
        """Accept exact role ARNs only."""
        return validate_plan_assume_role_arns(value)

    @field_validator("plan_role_arn")
    @classmethod
    def _validate_plan_role_arn(cls, value: Optional[str]) -> Optional[str]:
        """Accept an exact role ARN only."""
        return None if value is None else validate_plan_role_arn(value)


CLEARABLE_WORKSPACE_FIELDS: Final = (
    "run_role_arn",
    "pending_run_role_arn",
    "plan_role_arn",
    "working_directory",
    "description",
    "vcs_repo",
    "tracked_branch",
    "trigger_patterns",
    "speculative_plans",
    "file_triggers_enabled",
    "plan_assume_role_arns",
)
"""The update fields an explicit JSON null clears.

The body follows JSON Merge Patch: an omitted key leaves the stored value alone
and an explicit null removes the attribute from the row. Only these fields carry a
meaningful "not set" state. `engine` and `engine_version` are required on a stored
workspace, so a null on either is a validation error rather than a clear.
"""


class WorkspaceUpdate(BaseModel):
    """A partial workspace edit. The name and the id are not editable.

    A rename would break the state key, which is derived from the workspace id,
    and the `by_name` uniqueness claim at the same time, so it is refused by
    omission rather than by a check.

    The model separates "absent" from "explicitly null" by leaving every field
    unset by default and reading the body with `model_dump(exclude_unset=True)`,
    so a key only reaches the service when the request actually carried it. A null
    on one of `CLEARABLE_WORKSPACE_FIELDS` then means clear, which the service
    turns into a DynamoDB REMOVE.
    """

    engine: Optional[Engine] = None
    engine_version: Optional[str] = Field(default=None, min_length=1, max_length=32)
    run_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
    """Switch runs to this role at once, dropping any pending role."""
    pending_run_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
    """Stage a role to switch to once a verification run assumes it. Null discards it."""
    plan_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
    """Plan with this read only role instead of the run role. Null plans with the run role."""
    working_directory: Optional[str] = None
    description: Optional[str] = None
    vcs_repo: Optional[str] = Field(default=None, max_length=140, pattern=VCS_REPO_PATTERN)
    tracked_branch: Optional[str] = Field(default=None, min_length=1, max_length=255)
    trigger_patterns: Optional[list[str]] = Field(default=None, max_length=50)
    speculative_plans: Optional[bool] = None
    file_triggers_enabled: Optional[bool] = None
    plan_assume_role_arns: Optional[list[str]] = Field(default=None, max_length=PLAN_ASSUME_ROLE_ARNS_MAX)
    """Replace the roles a plan may assume. Null or an empty list leaves none."""

    @field_validator("working_directory")
    @classmethod
    def _normalize_working_directory(cls, value: Optional[str]) -> Optional[str]:
        """Store the directory as a clean relative path."""
        return None if value is None else normalize_working_directory(value)

    @field_validator("tracked_branch")
    @classmethod
    def _validate_branch(cls, value: Optional[str]) -> Optional[str]:
        """Refuse a name git would not accept as a branch."""
        return None if value is None else validate_branch(value)

    @field_validator("trigger_patterns")
    @classmethod
    def _validate_trigger_patterns(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        """Trim the patterns and refuse empty or oversized ones."""
        return None if value is None else validate_trigger_patterns(value)

    @field_validator("plan_assume_role_arns")
    @classmethod
    def _validate_plan_assume_role_arns(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        """Accept exact role ARNs only."""
        return None if value is None else validate_plan_assume_role_arns(value)

    @field_validator("plan_role_arn")
    @classmethod
    def _validate_plan_role_arn(cls, value: Optional[str]) -> Optional[str]:
        """Accept an exact role ARN only."""
        return None if value is None else validate_plan_role_arn(value)

    @model_validator(mode="before")
    @classmethod
    def _refuse_a_null_on_a_field_that_cannot_be_cleared(cls, data: Any) -> Any:
        """Reject an explicit null on a field a stored workspace has to carry.

        Every field defaults to `None` so that an omitted key stays unset, which is
        what keeps absent apart from null. That default makes `None` an accepted
        value on every field, so the two non-clearable fields are refused here instead
        of by their annotation. Without this a null on `engine_version` would be
        read as a clear of a required attribute, and silently dropped.
        """
        if not isinstance(data, dict):
            return data
        offenders = sorted(
            key
            for key, value in data.items()
            if value is None and key in cls.model_fields and key not in CLEARABLE_WORKSPACE_FIELDS
        )
        if offenders:
            raise ValueError(f"{', '.join(offenders)} cannot be cleared, so null is not an accepted value")
        return data


AwsConnectionStatus = Literal["waiting", "connected", "expired", "disconnected"]
"""Where a Quick setup stack is: link handed out, stack reported back, link unused
past its expiry, or stack deleted."""

AwsConnectionVerification = Literal["pending", "verified", "failed"]
"""Where the run that proves a reported role stands: still going, finished cleanly, or
ended without finishing. A stack reporting back only says the role exists; this is
whether a run could actually use it."""


class AwsConnection(BaseModel):
    """What the last Quick setup link and its stack reported, for the UI to follow live."""

    status: AwsConnectionStatus
    requested_at: Optional[datetime] = None
    """When the link was handed out."""
    expires_at: Optional[datetime] = None
    """When the link's connect token stops being accepted."""
    account_id: Optional[str] = None
    """The account the stack was created in, from the stack's own ARN."""
    role_arn: Optional[str] = None
    """The role the stack created."""
    plan_role_arn: Optional[str] = None
    """The read only plan role the stack created beside it, if any."""
    pending: bool = False
    """True when the role was staged beside a working one, waiting on its verification run."""
    stack_id: Optional[str] = None
    reported_at: Optional[datetime] = None
    """When the stack reported back."""
    run_id: Optional[str] = None
    """The verification run: the one started when the stack reported back, or the
    latest run on the role to settle a pending or failed verification."""
    verification: Optional[AwsConnectionVerification] = None
    """Whether that run proved the role. Absent on connections recorded before it existed."""
    verification_error: Optional[str] = None
    """Why the verification failed, when it did."""
    verified_at: Optional[datetime] = None
    """When the verification reached `verified` or `failed`."""
    disconnected_at: Optional[datetime] = None


class Workspace(WorkspaceBase):
    """A stored workspace, with everything the run role setup needs."""

    workspace_id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    run_role_setup: RunRoleSetup
    run_role_reconnect_required: bool = False
    """The role came from a Quick setup stack whose trust predates credential vending,
    so no run can assume it until the stack is updated or Quick setup is run again."""
    pending_run_role_arn: Optional[str] = None
    """A role waiting on its verification run. Runs keep using `run_role_arn` until
    the run role check sees the runner assume it and switches the workspace over."""
    run_role_checked_at: Optional[datetime] = None
    """When a run last proved the runner assumed the role, as of the last recorded check."""
    run_role_account_id: Optional[str] = None
    """The account the role ARN names, as of that recorded check."""
    vcs_repository_id: Optional[str] = None
    """The GitHub id of the bound repository, so the binding survives a rename.

    Resolved through the GitHub App when the repository is connected, or recorded on
    the first upload when the environment has no App. Cleared whenever `vcs_repo`
    changes to a repository that is not resolved."""
    vcs_installation_id: Optional[str] = None
    """The GitHub App installation that covered the repository when it was connected."""
    aws_connection: Optional[AwsConnection] = None
    """The last Quick setup link's progress, or `None` when none was handed out."""

    model_config = ConfigDict(from_attributes=True)


class WorkspaceList(BaseModel):
    """Every workspace, newest last by id, which is a ULID and so time ordered."""

    items: list[Workspace]


RunRoleCheckStatus = Literal["connected", "failed", "unverified"]
"""What the runner's own record says about a workspace's run role."""


class PendingRunRoleCheck(BaseModel):
    """What the runner's record says about the role waiting to replace the current one."""

    role_arn: str
    connected: bool
    """True once a verification run assumed this role and finished its plan."""
    status: RunRoleCheckStatus
    account_id: Optional[str] = None
    error: Optional[str] = None
    run_id: Optional[str] = None
    checked_at: Optional[datetime] = None


class RunRoleCheck(BaseModel):
    """Whether the runner can assume a workspace's run role, from its own record.

    The API never calls STS. The answer is the outcome of the runner's AssumeRole
    in the newest run created with the current role ARN that reached it, so a role
    no run has tried yet is `unverified` rather than refused.
    """

    connected: bool
    """True only when a run proved the runner assumed this role."""
    status: RunRoleCheckStatus
    account_id: Optional[str] = None
    """The account the role ARN names, when connected."""
    error: Optional[str] = None
    """A sentence for a person when not connected, saying what to fix or do next."""
    run_id: Optional[str] = None
    """The run the answer comes from, or `None` when unverified."""
    checked_at: Optional[datetime] = None
    """When that run reached its verdict."""
    pending: Optional[PendingRunRoleCheck] = None
    """The staged role and its own verdict, or `None` when no role is staged."""


RunRolePermissions = Literal["administrator", "power_user", "read_only", "none"]
"""The AWS managed policy the quick setup stack attaches: AdministratorAccess,
PowerUserAccess, ReadOnlyAccess, or none so a narrower policy can be attached by hand."""


class RunRoleQuickSetupCreate(BaseModel):
    """Start AWS quick setup for one workspace."""

    account_id: Optional[str] = Field(default=None, pattern=r"^\d{12}$")
    """The twelve digit AWS account the role is created in. Optional where the stack
    reports back, since the stack's own ARN names the account."""
    permissions: RunRolePermissions = "administrator"
    plan_role: bool = True
    """Also create a read only role for plans, so a plan never holds apply capable keys."""

    @model_validator(mode="before")
    @classmethod
    def _strip_separators(cls, data: Any) -> Any:
        """Accept the account id as the AWS console prints it, with dashes or spaces, and blank as none."""
        if isinstance(data, dict) and isinstance(data.get("account_id"), str):
            account_id: str = data["account_id"].replace("-", "").replace(" ", "")
            return {**data, "account_id": account_id or None}
        return data


class RunRoleQuickSetup(BaseModel):
    """The quick create link for a workspace's run role."""

    account_id: Optional[str] = None
    """The account given, or `None` when the stack reports its own."""
    role_arn: Optional[str] = None
    """The ARN saved or staged for the given account, or `None` until the stack reports back."""
    pending: bool = False
    """True when the workspace already runs as another role, so this one is staged as
    `pending_run_role_arn` and switched to only once a verification run assumes it."""
    role_name: str
    plan_role_name: Optional[str] = None
    """The read only plan role's name, under its IAM path, or `None` when none is created."""
    plan_role_arn: Optional[str] = None
    """The plan role ARN for the given account, or `None` until the stack reports back."""
    stack_name: str
    region: str
    """The region the CloudFormation console opens in. The role itself is global."""
    permissions_policy_arn: Optional[str] = None
    """The managed policy the stack attaches, or `None` when none was chosen."""
    console_url: str
    """The AWS CloudFormation quick create link. It embeds a presigned template URL."""
    expires_in: int
    """Seconds the embedded template URL stays readable, so the link must be used soon."""
    reports_back: bool = False
    """True when the stack reports its account and role back, so the workspace connects
    itself once the stack is created."""
    connect_expires_at: Optional[datetime] = None
    """When the link's one-time connect token stops being accepted."""


class VariableWrite(BaseModel):
    """A variable value being set. A sensitive value is never returned again."""

    value: str = Field(max_length=32_768)
    category: VariableCategory = "terraform"
    sensitive: bool = False
    hcl: bool = False
    """Parse `value` as an HCL expression rather than take it as a literal string.

    This is what makes a list or a map typed input variable expressible at all. It
    is refused on an `env` variable, because a process environment variable is a
    string to the process and there is nothing to parse it with.
    """
    description: str = ""


class Variable(BaseModel):
    """A stored variable as the API renders it.

    `value` is `None` for a sensitive variable, always, on every route. The
    runner reads the decrypted value through the run bundle, which is gated on a
    run token rather than on a human's scopes.
    """

    workspace_id: str
    key: str
    value: Optional[str] = None
    category: VariableCategory
    sensitive: bool
    hcl: bool = False
    """Whether the stored value is an HCL expression. A row written before this
    flag existed carries no such attribute and reads as `False`."""
    description: str = ""
    created_at: str
    updated_at: Optional[str] = None


class VariableList(BaseModel):
    """Every variable on one workspace, by key."""

    items: list[Variable]


class ConfigVersionCreate(BaseModel):
    """A request for somewhere to upload a config tarball."""

    size_bytes: int = Field(default=50_000_000, gt=0, le=250_000_000)
    """The ceiling the presigned PUT signs, which the client has to declare as
    `Content-Length` and S3 enforces at the header."""


class ConfigVersion(BaseModel):
    """A stored config version."""

    config_version_id: str
    workspace_id: str
    key: str
    status: Literal["pending", "uploaded"]
    size_bytes: int
    created_at: str
    updated_at: Optional[str] = None


class ConfigVersionUpload(BaseModel):
    """A new config version and the presigned PUT its tarball goes to.

    `headers` is not advisory: every one is inside the signature, so a request
    that omits or changes one is rejected by S3.
    """

    config_version: ConfigVersion
    upload_url: str
    headers: dict[str, str]
    expires_in: int


class ConfigVersionList(BaseModel):
    """One workspace's config versions, newest last by `created_at`."""

    items: list[ConfigVersion]


class StateVersion(BaseModel):
    """One version of a workspace's Terraform state, as the API renders it.

    A state body holds every resource attribute and every output in plaintext,
    routinely including passwords and private keys, so nothing derived from those
    appears here. The fields are the ones that describe the version rather than
    what is inside it, and the body is reachable only through the separate
    download route.
    """

    workspace_id: str
    state_version_id: str
    """The S3 object version holding this state. The history is S3's own object
    versioning rather than a copy, so this is the bucket's identifier."""
    created_at: str
    size_bytes: int
    is_current: bool
    """Whether this is the state a run would read now."""


class StateVersionDetail(StateVersion):
    """One state version with the three fields that only the state body carries.

    `serial`, `terraform_version` and `lineage` are read out of the object
    because Terraform writes state through its own backend and the control plane
    never gets to stamp them as S3 metadata. They are the only things lifted out
    of the body; no resource attribute and no output is ever read into a
    response. Each is optional because a body that cannot be parsed still has
    describable metadata.
    """

    serial: Optional[int] = None
    terraform_version: Optional[str] = None
    lineage: Optional[str] = None
    run_id: Optional[str] = None
    """The run that produced this state, when it is known. S3 records no such
    link, so this is populated only when a future write records it and reads as
    null for every state written before then."""


class StateVersionList(BaseModel):
    """One page of state versions, newest first. Follow the token even on empty pages.

    S3 delete markers count toward the page size but are not state versions.
    Malformed cursors or cursors from another workspace return HTTP 400.
    """

    items: list[StateVersion]
    next_page_token: Optional[str] = None
    """Opaque. Absent when this is the last page."""


class StateVersionDownload(BaseModel):
    """A short lived URL for one state version's raw bytes.

    The URL is a bearer credential for exactly one object version: it names the
    bucket, the key and the version inside its signature, so it cannot be steered
    at another workspace's state. It is minted only after the caller's scope and
    the version's ownership have both been checked, and it expires in
    `expires_in` seconds.
    """

    workspace_id: str
    state_version_id: str
    download_url: str
    expires_in: int
    size_bytes: int
