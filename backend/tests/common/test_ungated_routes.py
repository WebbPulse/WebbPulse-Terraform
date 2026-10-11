"""Every route the gateway serves without the access gate refuses a caller holding nothing.

`terraform/apigateway.tf` marks these routes `authorization_type = "NONE"`, so the
gate never sees them and the app's own check is the only one. Each is called here
with no credential, no signature and no body, and must refuse, apart from the
handshake and identity documents that are public by design. The set is read back
from the Terraform so a newly ungated route fails here until it is listed with the
answer its check gives; `.github/scripts/check_ungated_routes.py` holds the same
line on the Terraform side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TERRAFORM = Path(__file__).resolve().parents[3] / "terraform"

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"

PLACEHOLDERS = {
    "run_id": f"run-{ULID}",
    "plan_id": f"plan-{ULID}",
    "apply_id": f"apply-{ULID}",
    "workspace_id": f"ws-{ULID}",
    "config_version_id": f"cv-{ULID}",
    "state_version_id": f"sv-{ULID}version",
    "output_id": "wsout-b3V0cHV0",
    "token": "1.c2lnbmF0dXJl",
    "organization": "WebbPulse",
    "workspace_name": "demo",
    "namespace": "WebbPulse",
    "name": "demo",
    "provider": "aws",
    "type": "webbpulse",
    "version": "1.0.0",
    "os": "linux",
    "arch": "amd64",
}

REFUSED = 401
"""What a route guarded by a run token, a scoped key, a registry key or a signature answers."""

PUBLIC_ELSEWHERE = {
    "GET /api/auth/.well-known/jwks.json",
    "GET /api/auth/.well-known/openid-configuration",
    "GET /api/auth/oauth/providers",
}
"""Public identity documents, served by the identity router that `tests/common/identity` covers."""

ANONYMOUS_ANSWER: dict[str, int] = {
    "GET /api/v1/runs/{run_id}/bundle": REFUSED,
    "POST /api/v1/runs/{run_id}/artifact-uploads": REFUSED,
    "POST /api/v1/runs/{run_id}/heartbeat": REFUSED,
    "POST /api/v1/runs/{run_id}/credentials": REFUSED,
    "POST /api/v1/runs/{run_id}/phase-result": REFUSED,
    "POST /api/v1/runs/{run_id}/runner-token": REFUSED,
    "POST /api/v1/github/webhooks": REFUSED,
    "GET /v1/modules/{namespace}/{name}/{provider}/versions": REFUSED,
    "GET /v1/modules/{namespace}/{name}/{provider}/{version}/download": REFUSED,
    "GET /v1/providers/{namespace}/{type}/versions": REFUSED,
    "GET /v1/providers/{namespace}/{type}/{version}/download/{os}/{arch}": REFUSED,
    "POST /v1/oauth/token": 400,
    "GET /api/v2/ping": 204,
    "GET /api/v2/organizations/{organization}/entitlement-set": REFUSED,
    "GET /api/v2/organizations/{organization}/workspaces": REFUSED,
    "POST /api/v2/organizations/{organization}/workspaces": REFUSED,
    "GET /api/v2/organizations/{organization}/workspaces/{workspace_name}": REFUSED,
    "GET /api/v2/workspaces/{workspace_id}": REFUSED,
    "GET /api/v2/workspaces/{workspace_id}/all-vars": REFUSED,
    "POST /api/v2/workspaces/{workspace_id}/configuration-versions": REFUSED,
    "GET /api/v2/configuration-versions/{config_version_id}": REFUSED,
    "POST /api/v2/workspaces/{workspace_id}/actions/lock": REFUSED,
    "POST /api/v2/workspaces/{workspace_id}/actions/unlock": REFUSED,
    "POST /api/v2/workspaces/{workspace_id}/actions/force-unlock": REFUSED,
    "GET /api/v2/workspaces/{workspace_id}/current-state-version": REFUSED,
    "GET /api/v2/workspaces/{workspace_id}/current-state-version-outputs": REFUSED,
    "POST /api/v2/workspaces/{workspace_id}/state-versions": REFUSED,
    "GET /api/v2/state-versions/{state_version_id}": REFUSED,
    "GET /api/v2/state-versions/{state_version_id}/download": REFUSED,
    "GET /api/v2/state-version-outputs/{output_id}": REFUSED,
    "POST /api/v2/runs": REFUSED,
    "GET /api/v2/runs/{run_id}": REFUSED,
    "GET /api/v2/runs/{run_id}/run-events": REFUSED,
    "POST /api/v2/runs/{run_id}/actions/apply": REFUSED,
    "POST /api/v2/runs/{run_id}/actions/discard": REFUSED,
    "POST /api/v2/runs/{run_id}/actions/cancel": REFUSED,
    "GET /api/v2/workspaces/{workspace_id}/runs": REFUSED,
    "GET /api/v2/plans/{plan_id}": REFUSED,
    "GET /api/v2/plans/{plan_id}/logs/{token}": 404,
    "GET /api/v2/applies/{apply_id}": REFUSED,
    "GET /api/v2/applies/{apply_id}/logs/{token}": 404,
    "GET /api/v2/organizations/{organization}/runs/queue": REFUSED,
    "GET /api/v2/organizations/{organization}/capacity": REFUSED,
}

ROUTE_KEY = re.compile(r'"((?:GET|POST|PUT|PATCH|DELETE) /[^"]*)"\s*=\s*\{([^{}]*)\}')


def ungated_route_keys() -> set[str]:
    """Every route key the control plane API declares with `authorization_type = "NONE"`."""
    source = (TERRAFORM / "apigateway.tf").read_text()
    return {
        match.group(1)
        for match in ROUTE_KEY.finditer(source)
        if re.search(r'authorization_type\s*=\s*"NONE"', match.group(2))
    }


def concrete(route_key: str) -> tuple[str, str]:
    """The method and a well formed path for `route_key`, its parameters filled in."""
    method, template = route_key.split(" ", 1)
    return method, re.sub(r"\{(\w+)\}", lambda match: PLACEHOLDERS[match.group(1)], template)


def test_every_ungated_route_has_an_expected_anonymous_answer() -> None:
    """A route that leaves the gate is listed here, and a listed one still leaves it."""
    assert ungated_route_keys() == set(ANONYMOUS_ANSWER) | PUBLIC_ELSEWHERE


@pytest.mark.parametrize("route_key", sorted(ANONYMOUS_ANSWER))
def test_an_anonymous_caller_gets_the_routes_own_refusal(client, route_key: str) -> None:
    """With no credential the app's own check answers, and never with anything but a refusal or the handshake."""
    method, path = concrete(route_key)
    response = client.request(method, path)
    assert response.status_code == ANONYMOUS_ANSWER[route_key], (route_key, response.status_code, response.text[:200])


def test_the_handshake_answers_nothing_but_the_api_version(client) -> None:
    """`/api/v2/ping` is anonymous because go-tfe calls it first, so it carries no body."""
    response = client.get("/api/v2/ping")
    assert response.content == b""
    assert response.headers["TFP-API-Version"]
