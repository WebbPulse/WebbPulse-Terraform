"""Turning GitHub's repository archive into the tarball the registry serves.

The module tarball has the module at its root, since that is where `terraform init`
expects the root module. The rewrite and its limits are shared with the webhook
ingest in `common.github.repack`; the registry adds only that the root must hold a
`.tf` file. Every check that fails raises `InvalidModuleArchive`, which marks the
version `failed`, since retrying cannot change the bytes of a tagged commit.
"""

from __future__ import annotations

from pathlib import Path

from ...common.github import repack as shared
from ...common.github.repack import EXCLUDED_PARTS, MAX_MEMBERS, MAX_UNPACKED_BYTES

InvalidModuleArchive = shared.ArchiveRejected
"""The archive is not a module the registry will serve."""


def repack(source: Path, target: Path) -> None:
    """Rewrite GitHub's archive at `source` as the module tarball at `target`.

    Raises:
        InvalidModuleArchive: The first check that failed.
    """
    if shared.repack(source, target) == 0:
        raise InvalidModuleArchive("the repository root holds no .tf file")


__all__ = ["EXCLUDED_PARTS", "MAX_MEMBERS", "MAX_UNPACKED_BYTES", "InvalidModuleArchive", "repack"]
