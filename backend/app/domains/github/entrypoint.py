"""The GitHub domain's entrypoint, which is what this domain's Lambda runs."""

from typing import TYPE_CHECKING

from webbpulse.lambda_entry import run_uvicorn
from webbpulse.logging import configure_logging
from webbpulse.otel import configure_tracing, resolve_sample_ratio

from app.common.composition.settings import get_settings
from app.common.composition.wiring import DOMAINS, build_domain_app

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

DOMAIN = DOMAINS["github"]


def build_app() -> "FastAPI":
    """This domain's application: its routers and nothing else."""
    return build_domain_app(DOMAIN)


def main() -> None:
    """Configure logging and tracing, then serve this domain until killed.

    Logging first, then tracing, so a misconfigured function fails at cold start
    with the failure already in JSON. Tracing precedes the build because
    `create_app` instruments the app, and that instrumentation is skipped unless
    tracing is already enabled.
    """
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service=DOMAIN.service_name,
        environment=settings.environment,
    )
    configure_tracing(
        DOMAIN.service_name,
        environment=settings.environment,
        sample_ratio=resolve_sample_ratio(),
    )
    run_uvicorn(build_app())


if __name__ == "__main__":
    main()
