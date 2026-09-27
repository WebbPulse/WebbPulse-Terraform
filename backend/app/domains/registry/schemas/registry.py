"""Request and response models for the registry's upload and listing routes."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

MAX_MODULE_BYTES = 100_000_000
"""The ceiling on one module tarball."""

VersionStatus = Literal["pending", "published", "failed"]


class ModuleUploadCreate(BaseModel):
    """What the publishing workflow reports alongside its GitHub Actions OIDC token.

    The module and its version come from the verified token: the namespace is the
    repository owner, the name and provider come from the repository, and the
    version from the pushed tag.
    """

    size_bytes: int = Field(gt=0, le=MAX_MODULE_BYTES)
    """The tarball's exact length, which the presigned PUT signs as `Content-Length`."""


class ModuleAddress(BaseModel):
    """One module version's registry address."""

    namespace: str
    name: str
    provider: str
    version: str


class ModuleUpload(BaseModel):
    """Where to PUT the tarball.

    A retry of the same workflow run attempt gets the same `upload_id` and a freshly
    signed URL for the same key. Every header is inside the signature and has to be
    sent verbatim.
    """

    upload_id: str
    upload_url: str
    headers: dict[str, str]
    expires_in: int
    module: ModuleAddress


class ModuleVersion(BaseModel):
    """One version of a module and where its ingest stands."""

    version: str
    status: VersionStatus
    error: Optional[str] = None
    repository: str
    sha: str
    actor: str
    created_at: str
    published_at: Optional[str] = None
    size_bytes: Optional[int] = None


class Module(BaseModel):
    """One module and every version uploaded for it, newest first."""

    namespace: str
    name: str
    provider: str
    source: str
    """The address a module block's `source` names, without the registry host."""
    versions: list[ModuleVersion]


class ModuleList(BaseModel):
    """Every module in the registry."""

    modules: list[Module]


__all__ = [
    "MAX_MODULE_BYTES",
    "Module",
    "ModuleAddress",
    "ModuleList",
    "ModuleUpload",
    "ModuleUploadCreate",
    "ModuleVersion",
    "VersionStatus",
]
