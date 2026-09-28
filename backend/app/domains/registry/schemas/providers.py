"""Request and response models for the registry's provider routes."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

ProviderVersionStatus = Literal["pending", "published", "failed"]


class ProviderCreate(BaseModel):
    """A provider to connect to a GitHub repository the App is installed on.

    Like HCP Terraform's private provider publishing, driven from releases: each
    published GitHub release with a `vX.Y.Z` tag publishes that version, from the
    GoReleaser registry layout its assets carry. The namespace is the repository
    owner and the type comes from a `terraform-provider-<type>` repository name.
    """

    vcs_repo: str = Field(min_length=3, max_length=200, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    """The repository as `owner/name`, named `terraform-provider-<type>`."""
    import_releases: bool = True
    """Import the repository's existing releases; false leaves them for a resync."""


class ProviderPlatform(BaseModel):
    """One operating system and architecture a version ships a build for."""

    os: str
    arch: str
    filename: str
    shasum: str


class ProviderVersion(BaseModel):
    """One version of a provider and where its publishing stands."""

    version: str
    status: ProviderVersionStatus
    error: Optional[str] = None
    tag: str
    protocols: list[str]
    platforms: list[ProviderPlatform]
    key_id: Optional[str] = None
    """The id of the key whose signature over `SHA256SUMS` the registry checked."""
    actor: str
    created_at: str
    published_at: Optional[str] = None


class Provider(BaseModel):
    """One provider and every version published or attempted for it, newest first."""

    namespace: str
    type: str
    source: str
    """The address a `required_providers` entry names, without the registry host."""
    vcs_repo: str
    created_at: Optional[str] = None
    versions: list[ProviderVersion]


class ProviderList(BaseModel):
    """Every provider in the registry."""

    providers: list[Provider]


class ProviderSync(BaseModel):
    """A queued import of a provider's existing releases."""

    source: str
    delivery: str


__all__ = [
    "Provider",
    "ProviderCreate",
    "ProviderList",
    "ProviderPlatform",
    "ProviderSync",
    "ProviderVersion",
    "ProviderVersionStatus",
]
