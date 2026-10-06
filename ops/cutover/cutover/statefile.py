"""State identity checks and the private, shredded directory state bodies live in."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, cast

SHRED_CHUNK = 1024 * 1024


class StateError(Exception):
    """A state body that cannot be trusted for the move."""


class LineageMismatch(StateError):
    """Two states that are not the same history."""


class SerialMismatch(StateError):
    """Two states of one history at different points."""


@dataclass(frozen=True)
class StateMeta:
    """The only facts about a state the tool ever prints."""

    serial: int
    lineage: str
    terraform_version: str
    sha256: str

    def describe(self) -> str:
        """One printable line naming this state without any of its contents."""
        return (
            f"serial={self.serial} lineage={self.lineage} "
            f"terraform_version={self.terraform_version} sha256={self.sha256[:12]}"
        )


def parse_state(body: bytes) -> dict[str, Any]:
    """The decoded state document, or a `StateError` that never quotes the body."""
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StateError("state body is not JSON") from None
    if not isinstance(document, dict):
        raise StateError("state body is not a JSON object")
    return cast(dict[str, Any], document)


def parse_meta(body: bytes) -> StateMeta:
    """Serial, lineage, engine version and digest of a state body."""
    document = parse_state(body)
    serial = document.get("serial")
    lineage = document.get("lineage")
    version = document.get("terraform_version")
    if not isinstance(serial, int) or isinstance(serial, bool):
        raise StateError("state has no integer serial")
    if not isinstance(lineage, str) or not lineage:
        raise StateError("state has no lineage")
    if not isinstance(version, str) or not version:
        raise StateError("state has no terraform_version")
    return StateMeta(serial, lineage, version, hashlib.sha256(body).hexdigest())


def check_match(expected: StateMeta, actual: StateMeta, *, exact: bool = True) -> None:
    """Refuse unless `actual` is the same history as `expected`, at the same serial when `exact`."""
    if actual.lineage != expected.lineage:
        raise LineageMismatch(f"lineage {actual.lineage} does not match {expected.lineage}")
    if exact and actual.serial != expected.serial:
        raise SerialMismatch(f"serial {actual.serial} does not match {expected.serial}")
    if not exact and actual.serial < expected.serial:
        raise SerialMismatch(f"serial {actual.serial} is behind {expected.serial}")


def shred_file(path: Path) -> None:
    """Overwrite a file with zeros, flush it to disk and unlink it.

    Best effort: copy on write filesystems and SSD wear levelling may keep old blocks,
    which is why the directory is private and short lived as well.
    """
    try:
        size = path.stat().st_size
        with path.open("r+b", buffering=0) as handle:
            remaining = size
            while remaining > 0:
                step = min(remaining, SHRED_CHUNK)
                handle.write(b"\0" * step)
                remaining -= step
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return
    path.unlink(missing_ok=True)


def shred_tree(root: Path) -> None:
    """Shred every regular file under `root`, then remove the tree."""
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.is_file() and not candidate.is_symlink():
                shred_file(candidate)
    shutil.rmtree(root, ignore_errors=True)


class PrivateDir:
    """A mode 0700 temporary directory whose files are shredded on exit, even after an error."""

    def __init__(self, parent: Path | None = None, prefix: str = "cutover-") -> None:
        """Remember where to create the directory; nothing is created until entry."""
        self._parent = parent
        self._prefix = prefix
        self._path: Path | None = None

    @property
    def path(self) -> Path:
        """The directory, only while the context is open."""
        if self._path is None:
            raise RuntimeError("private directory is not open")
        return self._path

    def __enter__(self) -> PrivateDir:
        """Create the directory with owner only access."""
        old = os.umask(0o077)
        try:
            created = tempfile.mkdtemp(prefix=self._prefix, dir=self._parent)
        finally:
            os.umask(old)
        os.chmod(created, 0o700)
        self._path = Path(created)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Shred everything in the directory and remove it."""
        if self._path is not None:
            shred_tree(self._path)
            self._path = None

    def write(self, name: str, body: bytes) -> Path:
        """Create a new owner only file; refuses to replace one."""
        target = self.path / name
        if target.parent != self.path:
            raise ValueError("private files live directly in the private directory")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        return target

    def subdir(self, name: str) -> Path:
        """An owner only directory inside the private directory, for engine working trees."""
        target = self.path / name
        target.mkdir(mode=0o700)
        return target
