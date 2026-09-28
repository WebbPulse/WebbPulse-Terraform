"""Request and response models for the registry's module routes."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

MAX_MODULE_BYTES = 100_000_000
"""The ceiling on one module tarball."""

VersionStatus = Literal["pending", "published", "failed"]


class ModuleCreate(BaseModel):
    """A module to connect to a GitHub repository the App is installed on.

    Like HCP Terraform's publish from VCS: from then on a push of a `vX.Y.Z` or
    `X.Y.Z` tag to the repository publishes that version. The namespace is the
    repository owner. A name and provider left out come from a
    `terraform-<provider>-<name>` repository name.
    """

    vcs_repo: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    """The repository as `owner/name`."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    provider: Optional[str] = Field(default=None, min_length=1, max_length=64)
    import_tags: bool = True
    """Import the repository's existing `vX.Y.Z` and `X.Y.Z` tags; false leaves them for a resync."""


class ModuleVersion(BaseModel):
    """One version of a module and where its publishing stands."""

    version: str
    status: VersionStatus
    error: Optional[str] = None
    repository: str
    tag: Optional[str] = None
    sha: str
    actor: str
    created_at: str
    published_at: Optional[str] = None
    size_bytes: Optional[int] = None


class Module(BaseModel):
    """One module and every version published or attempted for it, newest first."""

    namespace: str
    name: str
    provider: str
    source: str
    """The address a module block's `source` names, without the registry host."""
    vcs_repo: Optional[str] = None
    """The connected repository; `None` for a module with versions but no connection."""
    created_at: Optional[str] = None
    versions: list[ModuleVersion]


class ModuleInput(BaseModel):
    """One input variable, as a module page lists it."""

    name: str
    type: Optional[str] = None
    description: Optional[str] = None
    default: Optional[str] = None
    """The default rendered as HCL; `None` when the input is required."""
    required: bool
    sensitive: bool = False


class ModuleOutput(BaseModel):
    """One output value."""

    name: str
    description: Optional[str] = None
    sensitive: bool = False


class ModuleProvider(BaseModel):
    """One provider requirement from `required_providers`."""

    name: str
    source: Optional[str] = None
    version: Optional[str] = None


class ModuleResource(BaseModel):
    """One managed resource the module declares."""

    type: str
    name: str


class Submodule(BaseModel):
    """A module under `modules/`, which a source reaches with `//modules/<name>`."""

    name: str
    path: str
    readme: Optional[str] = None
    inputs: list[ModuleInput]
    outputs: list[ModuleOutput]
    providers: list[ModuleProvider]
    resources: list[ModuleResource]


class ModuleDocs(BaseModel):
    """What a published version documents, read from its tarball when it published."""

    readme: Optional[str] = None
    inputs: list[ModuleInput]
    outputs: list[ModuleOutput]
    providers: list[ModuleProvider]
    resources: list[ModuleResource]
    submodules: list[Submodule]
    parse_errors: list[str]
    """Files left out because they did not parse or passed the size bounds."""


class ModuleVersionDetail(BaseModel):
    """One version of a module with its documentation, for the module page."""

    namespace: str
    name: str
    provider: str
    source: str
    vcs_repo: Optional[str] = None
    created_at: Optional[str] = None
    version: ModuleVersion
    docs: Optional[ModuleDocs] = None
    """`None` for a version not published, or whose documentation could not be read."""


class ModuleSync(BaseModel):
    """A queued import of a module's existing tags."""

    source: str
    delivery: str
    """The id the sync and every tag message it queues carry, for tracing."""


class ModuleList(BaseModel):
    """Every module in the registry."""

    modules: list[Module]


__all__ = [
    "MAX_MODULE_BYTES",
    "Module",
    "ModuleCreate",
    "ModuleDocs",
    "ModuleInput",
    "ModuleList",
    "ModuleOutput",
    "ModuleProvider",
    "ModuleResource",
    "ModuleSync",
    "ModuleVersion",
    "ModuleVersionDetail",
    "Submodule",
    "VersionStatus",
]
