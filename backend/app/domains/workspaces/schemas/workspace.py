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


class RunRoleSetup(BaseModel):
    """What a person needs to build the run role for one workspace.

    The role cannot exist before the workspace does: its trust policy names the
    workspace id as the external id, so the id has to be handed out first. Every
    workspace response carries these three values so the setup can be followed
    without reading the stack's outputs. The runner task roles are the only
    principals the trust policy needs: the API never assumes the role.
    """

    principal_arn: str
    """The first runner task role the run role's trust policy has to name."""
    principal_arns: list[str] = []
    """Every runner task role, one per phase. A trust policy naming only the plan
    role leaves the apply phase unable to assume, so all of them belong in it."""
    external_id: str
    """The workspace id, which the runner sends as `sts:ExternalId`."""
    role_name: str
    """The name the role has to carry to fall inside the runner's AssumeRole grant."""


class WorkspaceBase(BaseModel):
    """Fields every workspace representation carries."""

    engine: Engine = "terraform"
    engine_version: str = Field(min_length=1, max_length=32)
    run_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
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


CLEARABLE_WORKSPACE_FIELDS: Final = (
    "run_role_arn",
    "pending_run_role_arn",
    "working_directory",
    "description",
    "vcs_repo",
    "tracked_branch",
    "trigger_patterns",
    "speculative_plans",
    "file_triggers_enabled",
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
    working_directory: Optional[str] = None
    description: Optional[str] = None
    vcs_repo: Optional[str] = Field(default=None, max_length=140, pattern=VCS_REPO_PATTERN)
    tracked_branch: Optional[str] = Field(default=None, min_length=1, max_length=255)
    trigger_patterns: Optional[list[str]] = Field(default=None, max_length=50)
    speculative_plans: Optional[bool] = None
    file_triggers_enabled: Optional[bool] = None

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


class Workspace(WorkspaceBase):
    """A stored workspace, with everything the run role setup needs."""

    workspace_id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None
    run_role_setup: RunRoleSetup
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
    """True once a verification run proved the runner assumed this role."""
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

    account_id: str = Field(pattern=r"^\d{12}$")
    """The twelve digit AWS account the role is created in."""
    permissions: RunRolePermissions = "administrator"

    @model_validator(mode="before")
    @classmethod
    def _strip_separators(cls, data: Any) -> Any:
        """Accept the account id as the AWS console prints it, with dashes or spaces."""
        if isinstance(data, dict) and isinstance(data.get("account_id"), str):
            account_id: str = data["account_id"]
            return {**data, "account_id": account_id.replace("-", "").replace(" ", "")}
        return data


class RunRoleQuickSetup(BaseModel):
    """The quick create link for a workspace's run role, whose ARN is now saved or staged."""

    account_id: str
    role_arn: str
    """The ARN the stack's role will carry."""
    pending: bool = False
    """True when the workspace already runs as another role, so this one is staged as
    `pending_run_role_arn` and switched to only once a verification run assumes it."""
    role_name: str
    stack_name: str
    region: str
    """The region the CloudFormation console opens in. The role itself is global."""
    permissions_policy_arn: Optional[str] = None
    """The managed policy the stack attaches, or `None` when none was chosen."""
    console_url: str
    """The AWS CloudFormation quick create link. It embeds a presigned template URL."""
    expires_in: int
    """Seconds the embedded template URL stays readable, so the link must be used soon."""


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
