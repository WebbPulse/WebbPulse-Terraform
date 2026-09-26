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
    from app.domains.workspaces.router import router

    return [router, api_keys_router]


def _workspaces_unprefixed_routers(settings: Settings) -> "list[APIRouter]":
    """The shared package's identity router, which carries the issuer's own path.

    Identity is served by the workspaces function rather than a domain of its own,
    so the glue mounts here. It takes no prefix; a prefix would double every path to
    `/api/auth/api/auth/...`.

    Empty when `IDENTITY_ISSUER` is unset, so a deployment without an issuer builds
    none of the glue's AWS clients. The import is inside the body for the same
    reason every loader's is: the runs image must never reach this module.
    """
    if not settings.IDENTITY_ISSUER:
        return []

    from app.common.identity.package_glue import build_router

    return [build_router(settings)]


def _runs_routers() -> "list[APIRouter]":
    """Import and return the runs domain's routers."""
    from app.domains.runs.router import router

    return [router]


def _runs_unprefixed_routers(settings: Settings) -> "list[APIRouter]":
    """The consumers' one route, at the adapter's pass-through path.

    Unprefixed because a prefix would leave the event source mapping posting to a
    path the application does not serve, which every message would then fail on. One
    router for every consumer, because the adapter posts every queue invocation to
    the same pass-through path and `dispatch` routes each record on its `kind`.
    """
    from app.domains.runs.consumers.dispatch import build_router

    return [build_router(settings)]


def _github_routers() -> "list[APIRouter]":
    """Import and return the GitHub domain's routers."""
    from app.domains.github.router import router

    return [router]


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
        router_tags=("github",),
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
