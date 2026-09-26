"""Both composition roots, and the guarantee that they cannot drift.

Root A serves the whole surface in one process for local work and this suite.
Root B is what each Lambda runs, one domain per function. Both are built from
`wiring.DOMAINS`, and these tests are what hold that invariant: a domain added to
the map without an entrypoint, or mounted at the wrong prefix in one root, fails
here rather than in a deployed function.
"""

import pytest
from fastapi.testclient import TestClient

from app.common.composition.wiring import DOMAIN_NAMES, DOMAINS, build_domain_app

DOMAIN_MODULES = {
    "workspaces": "app.domains.workspaces.entrypoint",
    "runs": "app.domains.runs.entrypoint",
    "github": "app.domains.github.entrypoint",
}


def paths_of(app) -> set[str]:
    """Every route path the app serves, read after startup.

    FastAPI defers `include_router`, so routes materialise at startup rather than
    at import. Reading `app.routes` before then reports only the built-ins.
    """
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    return set(schema["paths"])


def test_the_map_carries_every_domain():
    """The contract's domains are the ones wired."""
    assert set(DOMAIN_NAMES) == {"workspaces", "runs", "github"}


@pytest.mark.parametrize("name", sorted(DOMAIN_MODULES))
def test_every_domain_has_an_entrypoint(name):
    """Each domain in the map has an importable entrypoint that builds an app."""
    import importlib

    module = importlib.import_module(DOMAIN_MODULES[name])
    assert module.DOMAIN is DOMAINS[name]
    assert paths_of(module.build_app())


def test_the_test_set_covers_the_whole_map():
    """A domain added to the map without a test module here fails this test."""
    assert set(DOMAIN_MODULES) == set(DOMAIN_NAMES)


@pytest.mark.parametrize("name", sorted(DOMAIN_MODULES))
def test_a_domain_app_serves_health(name, settings):
    """Each function answers the readiness path the Lambda Web Adapter polls.

    `AWS_LWA_READINESS_CHECK_PATH` is `/health`, so a function without it never
    reports ready and the deploy stalls rather than failing loudly.
    """
    with TestClient(build_domain_app(name, settings=settings)) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.parametrize("name", sorted(DOMAIN_MODULES))
def test_a_domain_app_serves_only_its_own_routes(name, settings):
    """One function carries its domain's routes and none of any other's."""
    served = paths_of(build_domain_app(name, settings=settings))
    theirs: set[str] = set()
    for other in DOMAIN_NAMES:
        if other != name:
            theirs |= {
                path for path in paths_of(build_domain_app(other, settings=settings)) if path.startswith("/api/v1")
            }

    own = {path for path in served if path.startswith("/api/v1")}
    assert own
    assert not own & theirs


def test_the_whole_surface_is_the_union_of_the_domains(app, settings):
    """Root A serves exactly what the domain functions serve together.

    This is the anti-drift assertion: a route reachable locally but on no
    function, or the reverse, is a deploy-time surprise and fails here instead.
    """
    whole = {path for path in paths_of(app) if path.startswith("/api/v1")}
    union: set[str] = set()
    for name in DOMAIN_NAMES:
        union |= {path for path in paths_of(build_domain_app(name, settings=settings)) if path.startswith("/api/v1")}
    assert whole == union


def test_every_route_mounts_under_the_contract_prefix(app):
    """Nothing escapes `/api/v1` except health and the schema documents."""
    unprefixed = {
        path for path in paths_of(app) if not path.startswith("/api/v1") and path not in {"/health", "/openapi.json"}
    }
    assert not unprefixed


def test_the_service_names_follow_the_template():
    """Each domain reports the service name Terraform sets for its function."""
    for name, domain in DOMAINS.items():
        assert domain.service_name == f"webbpulse-terraform-{name}"


def test_the_domain_header_names_the_serving_domain(settings):
    """A response says which domain served it, which is what makes a log traceable."""
    from app.common.core.middleware import DOMAIN_HEADER

    with TestClient(build_domain_app("runs", settings=settings)) as client:
        response = client.get("/health")
    assert response.headers[DOMAIN_HEADER] == "runs"
