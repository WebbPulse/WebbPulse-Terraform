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
    """Import and return the workspaces domain's routers."""
    from app.domains.workspaces.router import router

    return [router]


def _runs_routers() -> "list[APIRouter]":
    """Import and return the runs domain's routers."""
    from app.domains.runs.router import router

    return [router]


DOMAINS: Final[dict[str, Domain]] = {
    "workspaces": Domain(
        name="workspaces",
        title="WebbPulse Terraform workspaces",
        load_routers=_workspaces_routers,
        router_tags=("workspaces",),
    ),
    "runs": Domain(
        name="runs",
        title="WebbPulse Terraform runs",
        load_routers=_runs_routers,
        router_tags=("runs",),
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
