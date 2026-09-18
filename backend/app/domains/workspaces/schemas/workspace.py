"""Request and response models for workspaces, variables and config versions."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Engine = Literal["terraform", "tofu"]
"""Which binary runs this workspace. The runner image bundles both."""

VariableCategory = Literal["terraform", "env"]
"""A `terraform` variable becomes a `-var` on the command line; an `env` variable
becomes a process environment variable on the task."""


class WorkspaceBase(BaseModel):
    """Fields every workspace representation carries."""

    engine: Engine = "terraform"
    engine_version: str = Field(min_length=1, max_length=32)
    run_role_arn: str = Field(min_length=20, max_length=2048)
    working_directory: str = ""
    description: str = ""


class WorkspaceCreate(WorkspaceBase):
    """A new workspace. The name is unique across the environment."""

    name: str = Field(min_length=1, max_length=90, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorkspaceUpdate(BaseModel):
    """A partial workspace edit. The name and the id are not editable.

    A rename would break the state key, which is derived from the workspace id,
    and the `by_name` uniqueness claim at the same time, so it is refused by
    omission rather than by a check.
    """

    engine: Optional[Engine] = None
    engine_version: Optional[str] = Field(default=None, min_length=1, max_length=32)
    run_role_arn: Optional[str] = Field(default=None, min_length=20, max_length=2048)
    working_directory: Optional[str] = None
    description: Optional[str] = None


class Workspace(WorkspaceBase):
    """A stored workspace."""

    workspace_id: str
    name: str
    created_at: str
    updated_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class WorkspaceList(BaseModel):
    """Every workspace, newest last by id, which is a ULID and so time ordered."""

    items: list[Workspace]


class VariableWrite(BaseModel):
    """A variable value being set. A sensitive value is never returned again."""

    value: str = Field(max_length=32_768)
    category: VariableCategory = "terraform"
    sensitive: bool = False
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
