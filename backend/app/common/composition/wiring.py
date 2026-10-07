"""Domain descriptors and the builder that turns one into an application.

Both composition roots read this map, so the whole-surface application and a
deployed function cannot drift apart. Adding a domain means adding a row here and
a two line entrypoint, and both roots pick it up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Final

from webbpulse.http import create_app

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


@dataclass(frozen=True)
class Domain:
    """One deployable domain."""

    name: str
    title: str
    load_routers: Callable[[], "list[APIRouter]"]
    """Called lazily, so importing this module imports no domain package."""
    load_unprefixed_routers: Callable[[Settings], "list[APIRouter]"] | None = None
    """Routers that mount at the root rather than under `API_PREFIX`.

    Two kinds reach it. The consumer routes, because the Lambda Web Adapter posts a
    queue invocation to its own pass-through path, which is outside `/api/v1` and
    which the HTTP API never routes. And the identity router, because it carries the
    issuer's own `/api/auth` path and a prefix would double it.

    Called lazily with the resolved settings, for the same reason `load_routers` is."""
    router_prefix: str = API_PREFIX
    """Where this domain's routers mount, so both roots supply the same prefix."""
    router_tags: tuple[str, ...] = ()
    """Tags applied when the routers mount, shared by both roots."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Extra keyword arguments for `create_app`."""
    install_outer_middleware: Callable[["FastAPI", Settings], None] | None = None
    """Adds middleware outside every other layer, so it runs before routing and the app.

    Both roots call it last, which in Starlette makes it the outermost layer."""

    @property
    def service_name(self) -> str:
        """The name this domain's function reports to logs and traces."""
        return SERVICE_NAME_TEMPLATE.format(domain=self.name)


def _workspaces_routers() -> "list[APIRouter]":
    """Import and return the workspaces domain's routers.

    The API key routes ride on this domain because it already serves the identity
    router, so a key is minted by the same function that issued the session token
    minting it.
    """
    from app.domains.workspaces.api_keys_router import router as api_keys_router
    from app.domains.workspaces.notifications_router import router as notifications_router
    from app.domains.workspaces.projects_router import router as projects_router
    from app.domains.workspaces.router import router
    from app.domains.workspaces.terraform_login_router import router as login_router

    return [router, projects_router, api_keys_router, login_router, notifications_router]


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


DOMAINS: Final[dict[str, Domain]] = {
    "workspaces": Domain(
        name="workspaces",
        title="WebbPulse Terraform workspaces",
        load_routers=_workspaces_routers,
        load_unprefixed_routers=_workspaces_unprefixed_routers,
        router_tags=("workspaces",),
    ),
    "runs": Domain(
        name="runs",
        title="WebbPulse Terraform runs",
        load_routers=_runs_routers,
        load_unprefixed_routers=_runs_unprefixed_routers,
        router_tags=("runs",),
    ),
    "github": Domain(
        name="github",
        title="WebbPulse Terraform GitHub",
        load_routers=_github_routers,
        load_unprefixed_routers=_github_unprefixed_routers,
        router_tags=("github",),
        install_outer_middleware=_github_outer_middleware,
    ),
    "registry": Domain(
        name="registry",
        title="WebbPulse Terraform registry",
        load_routers=_registry_routers,
        load_unprefixed_routers=_registry_unprefixed_routers,
        router_tags=("registry",),
    ),
}

DOMAIN_NAMES: Final = tuple(DOMAINS)


def build_domain_app(domain: Domain | str, *, settings: Settings | None = None) -> "FastAPI":
    """Build one domain's application: root B's whole job, and root A's unit.

    Both roots go through here, so the middleware stack is identical locally, in
    the suite and in each deployed function.
    """
    from ..core.middleware import DomainHeaderMiddleware, TrailingSlashMiddleware

    resolved_domain = DOMAINS[domain] if isinstance(domain, str) else domain
    resolved = settings if settings is not None else get_settings()

    app = create_app(
        title=resolved_domain.title,
        version=VERSION,
        service_name=resolved_domain.service_name,
        settings=resolved,
        include_health=True,
        redirect_slashes=False,
        error_envelope=ERROR_ENVELOPE,
        **resolved_domain.extra,
    )

    for router in resolved_domain.load_routers():
        app.include_router(
            router,
            prefix=resolved_domain.router_prefix,
            tags=list(resolved_domain.router_tags),
        )

    if resolved_domain.load_unprefixed_routers is not None:
        for router in resolved_domain.load_unprefixed_routers(resolved):
            app.include_router(router)

    app.add_middleware(TrailingSlashMiddleware, router=app.router)
    app.add_middleware(DomainHeaderMiddleware, domain=resolved_domain.name)
    if resolved_domain.install_outer_middleware is not None:
        resolved_domain.install_outer_middleware(app, resolved)
    return app


__all__ = [
    "API_PREFIX",
    "DOMAINS",
    "DOMAIN_NAMES",
    "ERROR_ENVELOPE",
    "SERVICE_NAME_TEMPLATE",
    "Domain",
    "build_domain_app",
]
