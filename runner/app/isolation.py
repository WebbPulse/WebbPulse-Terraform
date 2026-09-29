"""Running the engine as its own user, apart from the runner that reports the phase.

Plan code is arbitrary code: an `external` data source or a provider runs whatever
the configuration names. The runner starts as root and runs every engine subcommand
as an unprivileged engine user, so nothing the engine starts can read the runner's
environment, memory or task credential endpoint, and so none of it can obtain the
run token or post a phase result. After each subcommand every process of the engine
user is killed, so nothing the plan left behind can rewrite an artifact before the
runner reads it.

The separation is on when `ENGINE_USER` names a user, which the image sets. With it
set a runner that is not root fails the phase rather than run the engine as itself.
Unset, the engine runs as the runner's own user, which is only for the suite.
"""

from __future__ import annotations

import os
import pwd
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ENGINE_USER_VARIABLE = "ENGINE_USER"
"""The variable naming the user the engine runs as."""

SWEEP_TIMEOUT_SECONDS = 30
"""How long one sweep of the engine user's processes may take."""

SWEEP_PROGRAM = (
    f"import os, signal\ntry:\n    os.kill(-1, {int(signal.SIGKILL)})\nexcept ProcessLookupError:\n    pass\n"
)
"""Kill every process the calling user may signal, which as the engine user is its own."""


class IsolationError(RuntimeError):
    """The engine user is named but cannot be used."""


@dataclass(frozen=True)
class EngineUser:
    """The unprivileged user every engine subcommand runs as."""

    name: str
    uid: int
    gid: int
    home: str

    def environment(self) -> dict[str, str]:
        """The identity variables the engine sees, so git and the engine write to its own home."""
        return {"HOME": self.home, "USER": self.name, "LOGNAME": self.name}


def engine_user(environ: Mapping[str, str] | None = None) -> EngineUser | None:
    """The engine user `ENGINE_USER` names, or None when it names none.

    Raises:
        IsolationError: The user does not exist, is root, or the runner is not root
            and so cannot run anything as another user.
    """
    source = os.environ if environ is None else environ
    name = source.get(ENGINE_USER_VARIABLE, "").strip()
    if not name:
        return None
    try:
        entry = pwd.getpwnam(name)
    except KeyError as error:
        raise IsolationError(f"the engine user {name} does not exist") from error
    if entry.pw_uid == 0:
        raise IsolationError("the engine user must not be root")
    if os.geteuid() != 0:
        raise IsolationError(f"the runner must start as root to run the engine as {name}")
    return EngineUser(name=name, uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir)


def hand_over(path: Path, user: EngineUser | None) -> None:
    """Give the engine user ownership of `path` and everything under it, never following a link."""
    if user is None:
        return
    os.chown(path, user.uid, user.gid, follow_symlinks=False)
    for root, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            os.chown(Path(root) / name, user.uid, user.gid, follow_symlinks=False)


def share(path: Path, group: int | None) -> None:
    """Let the engine user's group read `path` while the runner alone may write it."""
    if group is None:
        return
    os.chown(path, -1, group, follow_symlinks=False)
    path.chmod(0o750 if path.is_dir() else 0o640)


def sweep(user: EngineUser | None) -> None:
    """Kill every process of the engine user, so nothing the engine started outlives it."""
    if user is None:
        return
    subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", SWEEP_PROGRAM],
        user=user.uid,
        group=user.gid,
        extra_groups=[],
        env={},
        check=False,
        timeout=SWEEP_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
