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
}


def paths_of(app) -> set[str]:
    """Every route path the app serves, read after startup.

    FastAPI defers `include_router`, so routes materialise at startup rather than
    at import. Reading `app.routes` before then reports only the built-ins.
    """
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
    return set(schema["paths"])


def test_the_map_carries_both_domains():
    """The contract's two domains are the ones wired."""
    assert set(DOMAIN_NAMES) == {"workspaces", "runs"}


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
    """One function carries its domain's routes and not the other's."""
    served = paths_of(build_domain_app(name, settings=settings))
    other = next(item for item in DOMAIN_NAMES if item != name)
    other_paths = paths_of(build_domain_app(other, settings=settings))

    own = {path for path in served if path.startswith("/api/v1")}
    theirs = {path for path in other_paths if path.startswith("/api/v1")}
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


IDENTITY_GLUE_MODULE = "app.domains.identity.package_glue"

ISOLATION_PROGRAM = (
    "import sys;"
    "import app.domains.{package}.entrypoint as entrypoint;"
    "entrypoint.build_app();"
    "print(','.join(sorted(m for m in sys.modules if m.startswith('app.domains.'))))"
)


def _modules_imported_by(package: str) -> set[str]:
    """Every `app.domains.*` module a fresh process imports building that entrypoint.

    Run in a subprocess because this suite has already imported everything, so an
    in-process check would read the suite's imports rather than the image's.
    """
    import subprocess
    import sys as system

    result = subprocess.run(
        [system.executable, "-c", ISOLATION_PROGRAM.format(package=package)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return {module for module in result.stdout.strip().split(",") if module}


def test_the_runs_image_never_imports_the_identity_glue():
    """The runs function carries none of the identity code.

    Identity is served by the workspaces function alone. A runs image that imported
    the glue would carry the package's identity extra and its cold start cost, and
    would be granted nothing to run it against.
    """
    assert IDENTITY_GLUE_MODULE not in _modules_imported_by("runs")


def test_the_workspaces_image_does_not_import_the_runs_domain():
    """One function does not carry the other's code, identity mounting notwithstanding."""
    imported = _modules_imported_by("workspaces")
    assert not {module for module in imported if module.startswith("app.domains.runs")}


def test_the_workspaces_image_leaves_the_glue_unimported_without_an_issuer():
    """With no `IDENTITY_ISSUER` the glue is not imported at all.

    The loader returns early before its import, so a deployment before the identity
    module is in place builds none of the glue's AWS clients.
    """
    assert IDENTITY_GLUE_MODULE not in _modules_imported_by("workspaces")
