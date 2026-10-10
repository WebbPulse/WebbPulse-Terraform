"""Root A: every domain's routers on one application, in one process.

What the test suite and a local `uvicorn app.common.composition.app:app` run
against. Nothing deploys it: the domain functions serve every route in
production.

In the local environment the gateway's JWT authorizer is stood in for by the
shared package's `LocalAuthorizerMiddleware`, since without it a valid token
reaches every guarded route carrying no verified claims and each one answers 401.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .settings import Settings
from .wiring import DOMAINS, build_domain_app

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


def build_app(settings: Settings | None = None) -> "FastAPI":
    """Every domain's routers on one application.

    The include order is `wiring.DOMAINS`, so the `paths` map keeps a stable
    order between builds.
    """
    return build_domain_app(
        list(DOMAINS.values()),
        settings=settings,
        local_authorizer=True,
        title="WebbPulse Terraform control plane",
        service_name="webbpulse-terraform",
        description="Workspaces, variables, config versions and runs.",
    )


app = build_app()
