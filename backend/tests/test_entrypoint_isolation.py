"""Each domain function's image carries its own domain package and nothing else.

One Lambda is deployed per domain, so a domain package that reaches into another
drags that domain's code, its dependencies and its cold start cost into an image
that was never granted anything to run them against. `assert_entrypoint_isolation`
builds every entrypoint in a fresh interpreter and reports what it actually
imported, which is the only way to see what ships rather than what this suite
happens to have loaded.

No domain reaches into another, so `ALLOWED_FOREIGN` is empty and any cross-domain
import at all fails this test. A read two domains share is not a reason to import
the other's package: the workspace, variable and config version reads a run needs
live in `app.common.workspaces.reads`, which both images reach on their own.

The identity glue is checked here too. It lives under `app.common.` rather than
`app.domains.`, so the whole-registry check cannot see it: that check reports only
modules beneath its `package_root`. `entrypoint_imports` is pointed at `app.` for that
one assertion, which is the package's own probe rather than a subprocess of our own.

This spans every domain, so it lives in the shared shard rather than under
`tests/domains/<name>/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from webbpulse.testing import assert_entrypoint_isolation, entrypoint_imports

from app.common.composition.wiring import DOMAINS

BACKEND_ROOT: Final = Path(__file__).resolve().parents[1]

ALLOWED_FOREIGN: Final[dict[str, tuple[str, ...]]] = {}
"""Empty: no domain may import another's package, and none does."""

IDENTITY_GLUE_MODULE: Final = "app.common.identity.package_glue"

APP_ROOT: Final = "app."
"""The probe root for the glue assertion, wide enough to report `app.common` modules."""


def test_every_entrypoint_imports_only_its_own_domain() -> None:
    """No domain image reaches into another at all."""
    assert_entrypoint_isolation(
        DOMAINS,
        allowed_foreign=ALLOWED_FOREIGN,
        cwd=BACKEND_ROOT,
    )


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_no_entrypoint_imports_the_identity_glue(domain: str) -> None:
    """No function's image carries the identity glue, and so none builds its AWS clients.

    Identity is served by the workspaces function alone, and only once `IDENTITY_ISSUER`
    is set. The probe runs with a stripped environment, so neither image reaches the glue:
    the runs image never mounts it, and the workspaces loader returns before its import.
    """
    imported = entrypoint_imports(
        domain,
        module=f"app.domains.{domain}.entrypoint",
        package=domain,
        package_root=APP_ROOT,
        cwd=BACKEND_ROOT,
    ).imported
    assert IDENTITY_GLUE_MODULE not in imported
