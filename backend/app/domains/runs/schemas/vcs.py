"""Request and response models for the GitHub Actions upload route."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

SHA_PATTERN = r"^[0-9a-f]{40}$"
"""A full hex commit sha, as the workflow sends it."""

MAX_INGEST_BYTES = 250_000_000
"""The ceiling on one uploaded tarball, the same as a config version's."""


class VcsUploadCreate(BaseModel):
    """What the workflow reports alongside its GitHub Actions OIDC token.

    Only `size_bytes` is used as given. The repository, the event, the branch, the
    pull request number and the commit all come from the verified token, and
    `sha` and `base_sha` are recorded on a pull request as the workflow's
    unverified account of its head and base.
    """

    sha: str = Field(pattern=SHA_PATTERN)
    """The commit the tarball was built from. For a pull request, its head."""
    pr_number: Optional[int] = Field(default=None, ge=1)
    """Ignored. A pull request's number is read from the token's ref."""
    base_sha: Optional[str] = Field(default=None, pattern=SHA_PATTERN)
    size_bytes: int = Field(gt=0, le=MAX_INGEST_BYTES)
    """The tarball's exact length, which the presigned PUT signs as `Content-Length`."""


class VcsUpload(BaseModel):
    """Where to PUT the tarball.

    A retry of the same workflow run attempt gets the same `upload_id` and a freshly
    signed URL for the same key. Every header is inside the signature and has to be
    sent verbatim.
    """

    upload_id: str
    upload_url: str
    headers: dict[str, str]
    expires_in: int


__all__ = ["MAX_INGEST_BYTES", "SHA_PATTERN", "VcsUpload", "VcsUploadCreate"]
