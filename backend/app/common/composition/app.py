"""Root A: every domain's routers on one application, in one process.

What the test suite and a local `uvicorn app.common.composition.app:app` run
against. Nothing deploys it: the two domain functions serve every route in
production.

In the local environment the gateway's JWT authorizer is stood in for by the
shared package's `LocalAuthorizerMiddleware`, since without it a valid token
reaches every guarded route carrying no verified claims and each one answers 401.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from webbpulse.http import create_app
from webbpulse.identity import LOCAL_ENVIRONMENT, IdentitySettings, LocalAuthorizerMiddleware

from ..core.middleware import MONOLITH_DOMAIN, DomainHeaderMiddleware, TrailingSlashMiddleware
from ..version import VERSION
from .settings import Settings, get_settings
from .wiring import DOMAINS, ERROR_ENVELOPE

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


def _identity_settings() -> IdentitySettings:
    """Build `IdentitySettings` from the environment.

    Its `issuer` and `audience` have no defaults, so a type checker reads the
    zero argument call as missing them. Both come from the environment through
    pydantic-settings, which the checker cannot see, so the construction is
    narrowed to this one function rather than suppressed at the call site.
    """
    return IdentitySettings()  # pyright: ignore[reportCallIssue]


def build_app(settings: Settings | None = None) -> "FastAPI":
    """Every domain's routers on one application.

    The include order is `wiring.DOMAINS`, so the `paths` map keeps a stable
    order between builds.
    """
    resolved = settings if settings is not None else get_settings()

    app = create_app(
        title="WebbPulse Terraform control plane",
        version=VERSION,
        service_name="webbpulse-terraform",
        settings=resolved,
        include_health=True,
        description="Workspaces, variables, config versions and runs.",
        redirect_slashes=False,
        error_envelope=ERROR_ENVELOPE,
    )

    for domain in DOMAINS.values():
        for router in domain.load_routers():
            app.include_router(
                router,
                prefix=domain.router_prefix,
                tags=list(domain.router_tags),
            )

    if resolved.ENVIRONMENT.strip().lower() == LOCAL_ENVIRONMENT and resolved.IDENTITY_ISSUER:
        app.add_middleware(
            LocalAuthorizerMiddleware,
            settings=_identity_settings(),
            environment=resolved.ENVIRONMENT,
        )
    app.add_middleware(TrailingSlashMiddleware, router=app.router)
    app.add_middleware(DomainHeaderMiddleware, domain=MONOLITH_DOMAIN)
    return app


app = build_app()
