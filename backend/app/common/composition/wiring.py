"""The domain registry and the product's half of the builder.

Both composition roots read `DOMAINS`, so the whole-surface application and a
deployed function cannot drift apart. The descriptor and the builder itself are
`webbpulse.composition`'s; this module keeps the rows, which name this product's
routers, and the middleware only this product wires. Adding a domain means adding
a row here and a three line entrypoint, and both roots pick it up.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Final

from webbpulse.composition import AppConfigurer, Domain, DomainRegistry
from webbpulse.composition import build_domain_app as _build_domain_app

from ..version import VERSION
from .settings import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter, FastAPI

API_PREFIX: Final = "/api/v1"
"""Where every route mounts, as the contract fixes it."""

ERROR_ENVELOPE: Final = "detailed"
"""Error envelope shape shared by both roots, so the same failure renders the same
body whichever root served it."""

SERVICE_NAME_TEMPLATE: Final = "webbpulse-terraform-{domain}"
"""Service name pattern. Terraform sets `SERVICE_NAME` to the same string, which
becomes the OpenTelemetry `service.name` and the `service` log field."""

OUTER_MIDDLEWARE: Final = "outer_middleware"
"""The `Domain.metadata` key naming a hook that adds middleware outside every other layer.

The hook takes the application and the settings, and runs last, which in Starlette
makes what it adds the outermost layer, so it runs before routing and the app."""

type OuterMiddleware = Callable[["FastAPI", Settings], None]


def _workspaces_routers() -> "list[APIRouter]":
    """Import and return the workspaces domain's routers.

    The API key routes ride on this domain because it already serves the identity
    router, so a key is minted by the same function that issued the session token
    minting it.
    """
    from app.domains.workspaces.api_keys_router import router as api_keys_router
    from app.domains.workspaces.audit_router import router as audit_router
    from app.domains.workspaces.notifications_router import router as notifications_router
    from app.domains.workspaces.outputs_router import router as outputs_router
    from app.domains.workspaces.projects_router import router as projects_router
    from app.domains.workspaces.router import router
    from app.domains.workspaces.terraform_login_router import router as login_router

    return [router, outputs_router, projects_router, api_keys_router, login_router, notifications_router, audit_router]


def _workspaces_unprefixed_routers(settings: Settings) -> "list[APIRouter]":
    """The identity router, `terraform login`'s token route and the `tfe.v2` reads.

    Each carries its own paths: the token route is `/v1/oauth/token`, beside the
    registry protocols, because service discovery names it, and the `tfe.v2`
    routes sit under `/api/v2`, where discovery points the `cloud {}` block. Identity is served by
    the workspaces function rather than a domain of its own, so the glue mounts
    here. It takes no prefix; a prefix would double every path to
    `/api/auth/api/auth/...`.

    No identity router when `IDENTITY_ISSUER` is unset, so a deployment without
    an issuer builds none of the glue's AWS clients. The imports are inside the
    body for the same reason every loader's is: the runs image must never reach
    these modules.
    """
    from app.domains.workspaces.terraform_login_router import token_router
    from app.domains.workspaces.tfe_router import router as tfe_router
    from app.domains.workspaces.tfe_state_router import router as tfe_state_router

    if not settings.IDENTITY_ISSUER:
        return [token_router, tfe_router, tfe_state_router]

    from app.common.identity.package_glue import build_router

    return [build_router(settings), token_router, tfe_router, tfe_state_router]


def _runs_routers() -> "list[APIRouter]":
    """Import and return the runs domain's routers."""
    from app.domains.runs.router import router

    return [router]


def _runs_unprefixed_routers(settings: Settings) -> "list[APIRouter]":
    """The consumers' one route, at the adapter's pass-through path, and the runs share of `tfe.v2`.

    Unprefixed because a prefix would leave the event source mapping posting to a
    path the application does not serve, which every message would then fail on. One
    router for every consumer, because the adapter posts every queue invocation to
    the same pass-through path and `dispatch` routes each record on its `kind`. The
    `tfe.v2` router carries its own `/api/v2` prefix, the path go-tfe calls.
    """
    from app.domains.runs.consumers.dispatch import build_router
    from app.domains.runs.tfe_router import router as tfe_router

    return [build_router(settings), tfe_router]


def _github_routers() -> "list[APIRouter]":
    """Import and return the GitHub domain's routers."""
    from app.domains.github.router import router

    return [router]


def _github_unprefixed_routers(settings: Settings) -> "list[APIRouter]":
    """The webhook route, which carries its full path and no admin guard."""
    from app.domains.github.webhooks_router import router

    return [router]


def _github_outer_middleware(app: "FastAPI", settings: Settings) -> None:
    """The webhook signature gate, outside everything else on the github function."""
    from ..github.webhooks import WebhookSignatureMiddleware

    app.add_middleware(WebhookSignatureMiddleware, settings=settings)


def _registry_routers() -> "list[APIRouter]":
    """Import and return the registry domain's `/api/v1` routers."""
    from app.domains.registry.router import router

    return [router]


def _registry_unprefixed_routers(settings: Settings) -> "list[APIRouter]":
    """The module and provider registry protocols and the ingest consumer, all at the root.

    The protocols mount at `/v1/modules` and `/v1/providers`, where service discovery
    points Terraform, and the consumer at the adapter's pass-through path.
    """
    from app.domains.registry.consumers.dispatch import build_router
    from app.domains.registry.protocol_router import providers_router
    from app.domains.registry.protocol_router import router as protocol_router

    return [protocol_router, providers_router, build_router(settings)]


def _row(name: str, title: str, **fields: Any) -> Domain:
    """One registry row, with the prefix and service name every row shares."""
    return Domain(
        name=name,
        title=title,
        router_prefix=API_PREFIX,
        service_name_template=SERVICE_NAME_TEMPLATE,
        **fields,
    )


DOMAINS: Final = DomainRegistry(
    [
        _row(
            "workspaces",
            "WebbPulse Terraform workspaces",
            load_routers=_workspaces_routers,
            load_unprefixed_routers=_workspaces_unprefixed_routers,
            router_tags=("workspaces",),
        ),
        _row(
            "runs",
            "WebbPulse Terraform runs",
            load_routers=_runs_routers,
            load_unprefixed_routers=_runs_unprefixed_routers,
            router_tags=("runs",),
        ),
        _row(
            "github",
            "WebbPulse Terraform GitHub",
            load_routers=_github_routers,
            load_unprefixed_routers=_github_unprefixed_routers,
            router_tags=("github",),
            metadata={OUTER_MIDDLEWARE: _github_outer_middleware},
        ),
        _row(
            "registry",
            "WebbPulse Terraform registry",
            load_routers=_registry_routers,
            load_unprefixed_routers=_registry_unprefixed_routers,
            router_tags=("registry",),
        ),
    ]
)

DOMAIN_NAMES: Final = DOMAINS.names


def _install_local_authorizer(app: "FastAPI", settings: Settings) -> None:
    """Stand in for the gateway's JWT authorizer on a local stack with an issuer.

    Product glue rather than `webbpulse.composition.local_authorizer`, because this
    product's middleware also takes the environment and whether device grant tokens
    are accepted, which the shared hook does not pass.
    """
    from webbpulse.identity import LOCAL_ENVIRONMENT, LocalAuthorizerMiddleware

    if settings.ENVIRONMENT.strip().lower() != LOCAL_ENVIRONMENT or not settings.IDENTITY_ISSUER:
        return

    from ..identity.package_glue import build_identity_settings

    app.add_middleware(
        LocalAuthorizerMiddleware,
        settings=build_identity_settings(settings),
        environment=settings.ENVIRONMENT,
        accept_device_tokens=settings.IDENTITY_DEVICE_GRANT_ENABLED,
    )


def _product_middleware(settings: Settings, *, local_authorizer: bool) -> AppConfigurer:
    """The hook adding this product's middleware once every router is in.

    The order is the stack, innermost first: the local authorizer when asked for,
    the trailing slash redirect, the domain header naming the serving domain or the
    monolith, and last each domain's outer middleware.
    """
    from webbpulse.http import MONOLITH_DOMAIN, DomainHeaderMiddleware, TrailingSlashMiddleware

    def install(app: "FastAPI", domains: Sequence[Domain]) -> None:
        """Add the product middleware to one built application."""
        if local_authorizer:
            _install_local_authorizer(app, settings)
        app.add_middleware(TrailingSlashMiddleware, router=app.router)
        app.add_middleware(DomainHeaderMiddleware, domain=domains[0].name if len(domains) == 1 else MONOLITH_DOMAIN)
        for domain in domains:
            outer: OuterMiddleware | None = domain.metadata.get(OUTER_MIDDLEWARE)
            if outer is not None:
                outer(app, settings)

    return install


def build_domain_app(
    domains: Domain | str | Sequence[Domain | str],
    *,
    settings: Settings | None = None,
    local_authorizer: bool = False,
    **create_app_kwargs: Any,
) -> "FastAPI":
    """Build one domain's application, or several domains' on one: both roots' one builder.

    Both roots go through here, so the middleware stack is identical locally, in
    the suite and in each deployed function. `local_authorizer` is Root A's alone.
    """
    resolved = settings if settings is not None else get_settings()
    return _build_domain_app(
        domains,
        registry=DOMAINS,
        settings=resolved,
        after_routers=[_product_middleware(resolved, local_authorizer=local_authorizer)],
        instrument=True,
        version=VERSION,
        include_health=True,
        redirect_slashes=False,
        error_envelope=ERROR_ENVELOPE,
        **create_app_kwargs,
    )


__all__ = [
    "API_PREFIX",
    "DOMAINS",
    "DOMAIN_NAMES",
    "ERROR_ENVELOPE",
    "OUTER_MIDDLEWARE",
    "SERVICE_NAME_TEMPLATE",
    "build_domain_app",
]
