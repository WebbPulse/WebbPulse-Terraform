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

from webbpulse.http import MONOLITH_DOMAIN, DomainHeaderMiddleware, TrailingSlashMiddleware, create_app
from webbpulse.identity import LOCAL_ENVIRONMENT, LocalAuthorizerMiddleware

from ..identity.package_glue import build_identity_settings
from ..version import VERSION
from .settings import Settings, get_settings
from .wiring import DOMAINS, ERROR_ENVELOPE

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


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
        if domain.load_unprefixed_routers is not None:
            for router in domain.load_unprefixed_routers(resolved):
                app.include_router(router)

    if resolved.ENVIRONMENT.strip().lower() == LOCAL_ENVIRONMENT and resolved.IDENTITY_ISSUER:
        app.add_middleware(
            LocalAuthorizerMiddleware,
            settings=build_identity_settings(resolved),
            environment=resolved.ENVIRONMENT,
            accept_device_tokens=resolved.IDENTITY_DEVICE_GRANT_ENABLED,
        )
    app.add_middleware(TrailingSlashMiddleware, router=app.router)
    app.add_middleware(DomainHeaderMiddleware, domain=MONOLITH_DOMAIN)
    for domain in DOMAINS.values():
        if domain.install_outer_middleware is not None:
            domain.install_outer_middleware(app, resolved)
    return app


app = build_app()
