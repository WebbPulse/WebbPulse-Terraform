"""Each domain function's image carries its own domain package and nothing else.

One Lambda is deployed per domain, so a domain package that reaches into another
drags that domain's code, its dependencies and its cold start cost into an image
that was never granted anything to run them against. `assert_entrypoint_isolation`
builds every entrypoint in a fresh interpreter and reports what it actually
imported, which is the only way to see what ships rather than what this suite
happens to have loaded.

The single deliberate exception is `runs` importing `app.domains.workspaces.service`:
a run resolves its workspace and the config version it executes against through the
workspaces service, so that read is a real dependency of the runs image rather than
a leak. It is allowed for `runs` alone and only for that module, so the rest of the
workspaces domain still fails this test if it follows.

This spans every domain, so it lives in the shared shard rather than under
`tests/domains/<name>/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from webbpulse.testing import assert_entrypoint_isolation

from app.common.composition.wiring import DOMAINS

BACKEND_ROOT: Final = Path(__file__).resolve().parents[1]

ALLOWED_FOREIGN: Final[dict[str, tuple[str, ...]]] = {
    "runs": ("app.domains.workspaces.service",),
}


def test_every_entrypoint_imports_only_its_own_domain() -> None:
    """No domain image reaches into another, bar the `runs` workspace read."""
    assert_entrypoint_isolation(
        DOMAINS,
        allowed_foreign=ALLOWED_FOREIGN,
        cwd=BACKEND_ROOT,
    )
