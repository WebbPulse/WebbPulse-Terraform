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


class ModuleList(BaseModel):
    """Every module in the registry."""

    modules: list[Module]


__all__ = [
    "MAX_MODULE_BYTES",
    "Module",
    "ModuleCreate",
    "ModuleList",
    "ModuleVersion",
    "VersionStatus",
]
