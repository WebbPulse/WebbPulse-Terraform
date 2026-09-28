"""Localhost origins reach CORS only on a workstation stack.

A deployed API that allowed `http://localhost:5173` would answer credentialed
requests from any page a signed-in person happened to serve locally, so the dev
server's origins are asserted absent from every deployed environment.
"""

from __future__ import annotations

import pytest

from app.common.composition.settings import LOCALHOST_ORIGINS, Settings

SITE = "https://staging.terraform.webbpulse.com"


@pytest.mark.parametrize("environment", ["local", "dev", "development", "Development"])
def test_a_workstation_stack_allows_the_dev_server(environment: str) -> None:
    """Local and dev settings add the Vite origins to the configured ones."""
    settings = Settings(ENVIRONMENT=environment, CORS_ORIGINS=SITE)
    assert set(LOCALHOST_ORIGINS) <= set(settings.cors_allow_origins)
    assert SITE in settings.cors_allow_origins


@pytest.mark.parametrize("environment", ["staging", "production", "prod", "test", "stagign"])
def test_other_stacks_allow_only_the_configured_origins(environment: str) -> None:
    """Staging, prod, the suite and an unrecognised value carry no localhost origin."""
    settings = Settings(ENVIRONMENT=environment, CORS_ORIGINS=f"{SITE}, {SITE}")
    assert settings.cors_allow_origins == [SITE]
