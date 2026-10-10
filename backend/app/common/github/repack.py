"""Rewriting GitHub's repository archive with the repository at the tarball's root.

GitHub's archive wraps everything in one `owner-repo-sha/` directory. Both the
module registry and the webhook ingest strip it, keep only regular files and
directories, and leave out `.git`, `.terraform` and state files. The rewrite is
bounded in member count and unpacked size, so a hostile archive cannot fill the
function's temporary storage, and every check that fails raises `ArchiveRejected`,
since retrying cannot change the bytes of a commit.
"""

from __future__ import annotations

import posixpath
import tarfile
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Final, Optional

MAX_MEMBERS: Final = 10_000
MAX_UNPACKED_BYTES: Final = 500_000_000
EXCLUDED_PARTS: Final = frozenset({".git", ".terraform"})


class ArchiveRejected(Exception):
    """The archive breaks a rule of the rewrite: a path escapes, or a limit is passed."""


def relative(name: str, *, reserved: frozenset[str] = frozenset()) -> Optional[str]:
    """The member's path under GitHub's top level directory, or `None` to leave it out.

    A first path part in `reserved` is left out, so a repository cannot overwrite an
    entry the caller adds itself.

    Raises:
        ArchiveRejected: The path escapes the root.
    """
    parts = [part for part in name.split("/")[1:] if part]
    if not parts:
        return None
    if any(part in ("..", ".") for part in parts):
        raise ArchiveRejected(f"{name} escapes the root")
    if parts[0] in reserved or any(part in EXCLUDED_PARTS for part in parts):
        return None
    if parts[-1].endswith(".tfstate") or ".tfstate." in parts[-1]:
        return None
    normalised = posixpath.normpath("/".join(parts))
    if normalised.startswith("/") or normalised == ".." or normalised.startswith("../"):
        raise ArchiveRejected(f"{name} escapes the root")
    return normalised


def entry(name: str, *, size: int = 0, directory: bool = False, executable: bool = False) -> tarfile.TarInfo:
    """A normalised archive entry, owner and time stripped."""
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = 0o755 if directory or executable else 0o644
    info.size = 0 if directory else size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def repack(
    source: Path,
    target: Path,
    *,
    reserved: frozenset[str] = frozenset(),
    prelude: Iterable[tuple[tarfile.TarInfo, Optional[IO[bytes]]]] = (),
) -> int:
    """Rewrite GitHub's archive at `source` as a gzipped tar at `target`.

    `prelude` entries are written first, as given. Returns how many `.tf` files sit
    at the root, which the registry requires to be at least one.

    Raises:
        ArchiveRejected: The first check that failed, or a source that is not a
            gzipped tar.
    """
    members = 0
    unpacked = 0
    root_terraform = 0
    try:
        with (
            tarfile.open(source, "r|gz") as incoming,
            tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as outgoing,
        ):
            for info, handle in prelude:
                outgoing.addfile(info, handle)
            for member in incoming:
                members += 1
                if members > MAX_MEMBERS:
                    raise ArchiveRejected(f"the archive holds more than {MAX_MEMBERS} entries")
                name = relative(member.name, reserved=reserved)
                if name is None or not (member.isdir() or member.isfile()):
                    continue
                if member.isdir():
                    outgoing.addfile(entry(name, directory=True))
                    continue
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise ArchiveRejected(f"the archive unpacks to more than {MAX_UNPACKED_BYTES} bytes")
                handle = incoming.extractfile(member)
                if handle is None:
                    continue
                if "/" not in name and name.endswith(".tf"):
                    root_terraform += 1
                outgoing.addfile(entry(name, size=member.size, executable=bool(member.mode & 0o111)), handle)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ArchiveRejected(f"the archive is not a gzipped tar: {error}") from error
    return root_terraform


__all__ = ["EXCLUDED_PARTS", "MAX_MEMBERS", "MAX_UNPACKED_BYTES", "ArchiveRejected", "entry", "relative", "repack"]
