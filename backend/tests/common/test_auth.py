"""The auth chain and the scope guard on every route.

Each guarded route is asserted twice: refused with a key that is one scope short,
and allowed with exactly the scope it declares. A route that silently stopped
enforcing its scope would otherwise pass every other test in the suite.
"""

import pytest

from app.common.core.auth import (
    ALL_SCOPES,
    CONFIGS_READ,
    CONFIGS_WRITE,
    RUNNER_SCOPE,
    RUNS_APPLY,
    RUNS_READ,
    RUNS_WRITE,
    VARIABLES_READ,
    VARIABLES_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
)

WORKSPACE_BODY = {
    "name": "scoped",
    "engine": "terraform",
    "engine_version": "1.11.4",
    "run_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run",
}
VARIABLE_BODY = {"value": "us-west-2", "category": "terraform", "sensitive": False}


def routes(workspace_id: str, run_id: str) -> list[tuple[str, str, str, dict | None]]:
    """Every guarded route as (scope, method, path, body)."""
    return [
        (WORKSPACES_READ, "GET", "/api/v1/workspaces", None),
        (WORKSPACES_WRITE, "POST", "/api/v1/workspaces", WORKSPACE_BODY),
        (WORKSPACES_READ, "GET", f"/api/v1/workspaces/{workspace_id}", None),
        (WORKSPACES_WRITE, "PATCH", f"/api/v1/workspaces/{workspace_id}", {"description": "x"}),
        (WORKSPACES_WRITE, "DELETE", f"/api/v1/workspaces/{workspace_id}", None),
        (VARIABLES_READ, "GET", f"/api/v1/workspaces/{workspace_id}/variables", None),
        (VARIABLES_WRITE, "PUT", f"/api/v1/workspaces/{workspace_id}/variables/region", VARIABLE_BODY),
        (VARIABLES_READ, "GET", f"/api/v1/workspaces/{workspace_id}/variables/region", None),
        (VARIABLES_WRITE, "DELETE", f"/api/v1/workspaces/{workspace_id}/variables/region", None),
        (CONFIGS_READ, "GET", f"/api/v1/workspaces/{workspace_id}/config-versions", None),
        (CONFIGS_WRITE, "POST", f"/api/v1/workspaces/{workspace_id}/config-versions", {"size_bytes": 1024}),
        (RUNS_READ, "GET", f"/api/v1/runs?workspace_id={workspace_id}", None),
        (RUNS_READ, "GET", f"/api/v1/runs/{run_id}", None),
        (RUNS_READ, "GET", f"/api/v1/runs/{run_id}/logs", None),
        (RUNS_WRITE, "POST", "/api/v1/runs", {"workspace_id": workspace_id, "config_version_id": "cv-x"}),
        (RUNS_WRITE, "POST", f"/api/v1/runs/{run_id}/cancel", None),
        (RUNS_WRITE, "POST", f"/api/v1/runs/{run_id}/discard", None),
        (RUNS_APPLY, "POST", f"/api/v1/runs/{run_id}/confirm", None),
    ]


def call(test_client, method: str, path: str, body: dict | None):
    """Issue `method path` with an optional JSON body."""
    return test_client.request(method, path, json=body)


@pytest.mark.parametrize("method,path,body", [(m, p, b) for _, m, p, b in routes("ws-x", "run-x")])
def test_every_guarded_route_refuses_an_anonymous_caller(client, method, path, body):
    """No route on the surface answers without a credential."""
    assert call(client, method, path, body).status_code == 401


def test_every_guarded_route_refuses_a_key_missing_its_scope(scoped_client, workspace, created_run):
    """A key holding every scope but the route's own is refused.

    Parametrised inside one test so each route is checked against a key built
    from `ALL_SCOPES` minus exactly that route's scope, which is what catches a
    route guarded by the wrong constant.
    """
    workspace_id = workspace["workspace_id"]
    run_id = created_run["run_id"]
    refused: list[str] = []

    for scope, method, path, body in routes(workspace_id, run_id):
        insufficient = tuple(item for item in ALL_SCOPES if item != scope)
        with scoped_client(*insufficient) as caller:
            response = call(caller, method, path, body)
        if response.status_code != 403:
            refused.append(f"{method} {path} returned {response.status_code}, expected 403")

    assert not refused, "\n".join(refused)


def test_every_guarded_route_accepts_its_own_scope(scoped_client, workspace, created_run):
    """A key holding only the route's declared scope is let through.

    Asserted as not-401 and not-403: the route may still 404 or 409 on the
    fixture data, which is a different concern from whether the guard opened.
    """
    workspace_id = workspace["workspace_id"]
    run_id = created_run["run_id"]
    blocked: list[str] = []

    for scope, method, path, body in routes(workspace_id, run_id):
        with scoped_client(scope) as caller:
            response = call(caller, method, path, body)
        if response.status_code in (401, 403):
            blocked.append(f"{method} {path} returned {response.status_code} for {scope}")

    assert not blocked, "\n".join(blocked)


def test_a_read_scope_cannot_write(scoped_client, workspace):
    """A read-only key cannot create a workspace."""
    with scoped_client(WORKSPACES_READ, VARIABLES_READ, CONFIGS_READ, RUNS_READ) as caller:
        response = caller.post("/api/v1/workspaces", json=WORKSPACE_BODY)
    assert response.status_code == 403


def test_runs_write_cannot_confirm(scoped_client, awaiting_confirmation):
    """Applying needs `runs:apply`, which `runs:write` does not imply.

    The contract splits them so that queueing a plan and approving one are
    separately grantable. A key that could confirm with only `runs:write` would
    collapse that distinction.
    """
    run_id = awaiting_confirmation["run_id"]
    with scoped_client(RUNS_READ, RUNS_WRITE) as caller:
        response = caller.post(f"/api/v1/runs/{run_id}/confirm")
    assert response.status_code == 403


def test_runs_apply_can_confirm(scoped_client, awaiting_confirmation):
    """`runs:apply` is sufficient to confirm on its own."""
    run_id = awaiting_confirmation["run_id"]
    with scoped_client(RUNS_APPLY) as caller:
        response = caller.post(f"/api/v1/runs/{run_id}/confirm")
    assert response.status_code == 200, response.text


def test_the_runner_scope_is_not_a_human_scope():
    """`runner` is absent from the scopes a person or agent can hold.

    If it leaked into `ALL_SCOPES` then any fully scoped agent key would open
    every run's bundle, and the run token binding would be decorative.
    """
    assert RUNNER_SCOPE not in ALL_SCOPES


def test_a_runner_token_cannot_reach_a_human_route(runner_client, workspace):
    """A run token is refused on the routes a person uses.

    The token is deliberately narrow: it exists to serve one run's bundle, not to
    read or change the workspace it belongs to.
    """
    response = runner_client.get(f"/api/v1/workspaces/{workspace['workspace_id']}")
    assert response.status_code == 403


def test_a_revoked_key_stops_working(app, scoped_client, workspace):
    """Revoking a key takes effect on the next request.

    Scopes are not intersected against a membership store, so revocation is the
    only way to withdraw a key's authority and it has to be immediate.
    """
    from webbpulse.identity.api_keys import mint, verify

    from app.common.core.auth import RUN_TOKEN_TENANT, api_key_store

    store = api_key_store()
    minted = mint(
        user_id="user-revoked",
        tenant_id=RUN_TOKEN_TENANT,
        scopes=(WORKSPACES_READ,),
        store=store,
    )
    from fastapi.testclient import TestClient

    with TestClient(app, headers={"Authorization": f"Bearer {minted.plaintext}"}) as caller:
        assert caller.get("/api/v1/workspaces").status_code == 200
        store.revoke(minted.record.key_hash)
        assert caller.get("/api/v1/workspaces").status_code == 401

    assert verify(minted.plaintext, store) is None


def test_a_malformed_bearer_is_refused(app):
    """A bearer that is not a key at all is a 401, not a 500."""
    from fastapi.testclient import TestClient

    with TestClient(app, headers={"Authorization": "Bearer not-even-a-wpk-key"}) as caller:
        assert caller.get("/api/v1/workspaces").status_code == 401


def test_the_health_route_needs_no_credential(client):
    """Health is unguarded, because the readiness check has no credential."""
    assert client.get("/health").status_code == 200
