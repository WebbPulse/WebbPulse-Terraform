"""Turning GitHub's repository archive into the tarball the registry serves.

GitHub's archive wraps everything in one `owner-repo-sha/` directory. The module
tarball has the module at its root, since that is where `terraform init` expects
the root module, and it holds only regular files and directories: links, `.git`,
`.terraform` and state files are left out. Every check that fails raises
`InvalidModuleArchive`, which marks the version `failed`, since retrying cannot
change the bytes of a tagged commit.
"""

from __future__ import annotations

import posixpath
import tarfile
from pathlib import Path
from typing import Final, Optional

MAX_MEMBERS: Final = 10_000
MAX_UNPACKED_BYTES: Final = 500_000_000
EXCLUDED_PARTS: Final = frozenset({".git", ".terraform"})


class InvalidModuleArchive(Exception):
    """The archive is not a module the registry will serve."""


def _relative(name: str) -> Optional[str]:
    """The member's path under GitHub's top level directory, or `None` to leave it out."""
    parts = [part for part in name.split("/")[1:] if part]
    if not parts:
        return None
    if any(part in ("..", ".") for part in parts):
        raise InvalidModuleArchive(f"{name} escapes the module root")
    if any(part in EXCLUDED_PARTS for part in parts):
        return None
    if parts[-1].endswith(".tfstate") or ".tfstate." in parts[-1]:
        return None
    normalised = posixpath.normpath("/".join(parts))
    if normalised.startswith("/") or normalised == ".." or normalised.startswith("../"):
        raise InvalidModuleArchive(f"{name} escapes the module root")
    return normalised


def _member(name: str, *, size: int = 0, directory: bool = False, executable: bool = False) -> tarfile.TarInfo:
    """A normalised archive entry, owner and time stripped."""
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = 0o755 if directory or executable else 0o644
    info.size = 0 if directory else size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def repack(source: Path, target: Path) -> None:
    """Rewrite GitHub's archive at `source` as the module tarball at `target`.

    Bounded in member count and unpacked size, and the root must hold at least one
    `.tf` file.

    Raises:
        InvalidModuleArchive: The first check that failed.
    """
    members = 0
    unpacked = 0
    root_terraform = 0
    try:
        with (
            tarfile.open(source, "r|gz") as incoming,
            tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as outgoing,
        ):
            for member in incoming:
                members += 1
                if members > MAX_MEMBERS:
                    raise InvalidModuleArchive(f"the archive holds more than {MAX_MEMBERS} entries")
                name = _relative(member.name)
                if name is None or not (member.isdir() or member.isfile()):
                    continue
                if member.isdir():
                    outgoing.addfile(_member(name, directory=True))
                    continue
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise InvalidModuleArchive(f"the archive unpacks to more than {MAX_UNPACKED_BYTES} bytes")
                handle = incoming.extractfile(member)
                if handle is None:
                    continue
                if "/" not in name and name.endswith(".tf"):
                    root_terraform += 1
                outgoing.addfile(_member(name, size=member.size, executable=bool(member.mode & 0o111)), handle)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise InvalidModuleArchive(f"the archive is not a gzipped tar: {error}") from error
    if root_terraform == 0:
        raise InvalidModuleArchive("the repository root holds no .tf file")


__all__ = ["EXCLUDED_PARTS", "MAX_MEMBERS", "MAX_UNPACKED_BYTES", "InvalidModuleArchive", "repack"]
