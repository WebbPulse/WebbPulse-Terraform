"""Request and response models for workspaces, variables and config versions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

Engine = Literal["terraform", "tofu"]
"""Which binary runs this workspace. The runner image bundles both."""

VariableCategory = Literal["terraform", "env"]
"""A `terraform` variable becomes a `-var` on the command line; an `env` variable
becomes a process environment variable on the task."""


class RunRoleSetup(BaseModel):
    """What a person needs to build the run role for one workspace.

    The role cannot exist before the workspace does: its trust policy names the
    workspace id as the external id, so the id has to be handed out first. Every
    workspace response carries these three values so the setup can be followed
    without reading the stack's outputs.
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


class WorkspaceCreate(WorkspaceBase):
    """A new workspace. The name is unique across the environment."""

    name: str = Field(min_length=1, max_length=90, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


CLEARABLE_WORKSPACE_FIELDS: Final = ("run_role_arn", "working_directory", "description")
"""The update fields an explicit JSON null clears.

The body follows JSON Merge Patch: an omitted key leaves the stored value alone
and an explicit null removes the attribute from the row. Only these three carry a
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
    working_directory: Optional[str] = None
    description: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_a_null_on_a_field_that_cannot_be_cleared(cls, data: Any) -> Any:
        """Reject an explicit null on a field a stored workspace has to carry.

        Every field defaults to `None` so that an omitted key stays unset, which is
        what keeps absent apart from null. That default makes `None` an accepted
        value on all five, so the two non-clearable fields are refused here instead
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
    run_role_checked_at: Optional[datetime] = None
    """When the run role last answered an AssumeRole, or `None` when it never has."""
    run_role_account_id: Optional[str] = None
    """The account the run role resolved to on that check."""

    model_config = ConfigDict(from_attributes=True)


class WorkspaceList(BaseModel):
    """Every workspace, newest last by id, which is a ULID and so time ordered."""

    items: list[Workspace]


class RunRoleCheck(BaseModel):
    """The outcome of one AssumeRole against a workspace's run role.

    `error` is a sentence for a person rather than the STS code, and no part of
    the temporary credentials reaches it.
    """

    connected: bool
    account_id: Optional[str] = None
    error: Optional[str] = None


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
