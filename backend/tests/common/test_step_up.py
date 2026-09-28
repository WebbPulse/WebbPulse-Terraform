"""The step-up gate on every sensitive route: stale logins refused, fresh ones and keys let in.

A person arrives with the claims the JWT authorizer produced, stamped with an
`auth_time`; an agent arrives with a `wpk_` key, which has no login to age and passes.
Each gated route is driven three ways, and what is asserted for the fresh login and the
key is only that the gate let the request through, since the routes' own answers are
covered by their domains' tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from webbpulse.identity.scopes import STEP_UP_REQUIRED_ERROR_CODE

from app.common.core.auth import ALL_SCOPES, STEP_UP_MAX_AGE_SECONDS
from tests.conftest import mint_key, person_headers, seed_user

PERSON = "user-step-up"
NEW_ROLE = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-other"
MISSING_KEY_ID = "0" * 64


@dataclass(frozen=True)
class Call:
    """One request against a gated route, built once the workspace and run exist."""

    method: str
    path: str
    body: dict[str, Any] | None = None


def person(app: Any, *, auth_age: int | None) -> TestClient:
    """A client for an admin person who signed in `auth_age` seconds ago."""
    seed_user(PERSON)
    return TestClient(
        app,
        headers=person_headers(user_id=PERSON, scopes=ALL_SCOPES, roles=("admin",), auth_age=auth_age),
    )


def agent(app: Any) -> TestClient:
    """A client holding an agent key with every scope."""
    return TestClient(app, headers={"Authorization": f"Bearer {mint_key(*ALL_SCOPES)}"})


def send(client: TestClient, call: Call) -> Any:
    """Send `call` through `client`."""
    return client.request(call.method, call.path, json=call.body)


def assert_step_up_required(response: Any) -> None:
    """The refusal carries the code, the max age and the RFC 9470 challenge."""
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error_code"] == STEP_UP_REQUIRED_ERROR_CODE
    assert body["max_age"] == STEP_UP_MAX_AGE_SECONDS
    challenge = response.headers["WWW-Authenticate"]
    assert 'error="insufficient_user_authentication"' in challenge
    assert f"max_age={STEP_UP_MAX_AGE_SECONDS}" in challenge


def assert_let_through(response: Any) -> None:
    """The gate did not refuse; whatever the route answered is its own business."""
    if response.status_code == 401:
        assert response.json().get("error_code") != STEP_UP_REQUIRED_ERROR_CODE, response.text
    assert response.status_code not in (401, 403), response.text


def _workspace_path(workspace: dict[str, Any]) -> str:
    """The workspace's API path."""
    return f"/api/v1/workspaces/{workspace['workspace_id']}"


def _seed_sensitive(auth_client: TestClient, workspace: dict[str, Any], key: str) -> None:
    """Store a sensitive variable through an agent key, which the gate lets through."""
    response = auth_client.put(
        f"{_workspace_path(workspace)}/variables/{key}",
        json={"value": "secret", "sensitive": True},
    )
    assert response.status_code == 200, response.text


GATED: dict[str, Callable[[TestClient, dict[str, Any], dict[str, Any]], Call]] = {
    "mint an API key": lambda *_: Call("POST", "/api/v1/api-keys", {"name": "an-agent"}),
    "revoke an API key": lambda *_: Call("DELETE", f"/api/v1/api-keys/{MISSING_KEY_ID}"),
    "delete a workspace": lambda _c, ws, _r: Call("DELETE", _workspace_path(ws)),
    "run role quick setup": lambda _c, ws, _r: Call("POST", f"{_workspace_path(ws)}/run-role/quick-setup", {}),
    "change the run role": lambda _c, ws, _r: Call("PATCH", _workspace_path(ws), {"run_role_arn": NEW_ROLE}),
    "clear the run role": lambda _c, ws, _r: Call("PATCH", _workspace_path(ws), {"run_role_arn": None}),
    "stage a run role": lambda _c, ws, _r: Call("PATCH", _workspace_path(ws), {"pending_run_role_arn": NEW_ROLE}),
    "create a sensitive variable": lambda _c, ws, _r: Call(
        "PUT", f"{_workspace_path(ws)}/variables/fresh", {"value": "v", "sensitive": True}
    ),
    "overwrite a sensitive variable": lambda c, ws, _r: (
        _seed_sensitive(c, ws, "held"),
        Call("PUT", f"{_workspace_path(ws)}/variables/held", {"value": "plain", "sensitive": False}),
    )[1],
    "delete a sensitive variable": lambda c, ws, _r: (
        _seed_sensitive(c, ws, "gone"),
        Call("DELETE", f"{_workspace_path(ws)}/variables/gone"),
    )[1],
    "confirm a run": lambda _c, _ws, run: Call("POST", f"/api/v1/runs/{run['run_id']}/confirm", {}),
    "connect a registry module": lambda *_: Call(
        "POST", "/api/v1/registry/modules", {"vcs_repo": "acme/terraform-aws-vpc", "import_tags": False}
    ),
    "delete a registry module": lambda *_: Call("DELETE", "/api/v1/registry/modules/acme/vpc/aws"),
    "start the GitHub App manifest": lambda *_: Call("POST", "/api/v1/github/app/manifest", {}),
    "sync the GitHub webhook": lambda *_: Call("POST", "/api/v1/github/app/webhook"),
    "start a GitHub install": lambda *_: Call("POST", "/api/v1/github/install-state"),
    "forget a GitHub installation": lambda *_: Call("DELETE", "/api/v1/github/installations/1"),
}


@pytest.fixture
def build_call(auth_client, workspace, awaiting_confirmation):
    """Resolve one gated route's request against a real workspace and a run awaiting confirmation."""

    def build(name: str) -> Call:
        """The request for the route called `name`."""
        return GATED[name](auth_client, workspace, awaiting_confirmation)

    return build


@pytest.mark.parametrize("name", sorted(GATED))
def test_a_stale_login_must_step_up(app, build_call, name):
    """A login older than the window is refused with the step-up challenge."""
    call = build_call(name)
    with person(app, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as client:
        assert_step_up_required(send(client, call))


@pytest.mark.parametrize("name", sorted(GATED))
def test_an_undated_login_must_step_up(app, build_call, name):
    """A token with no `auth_time`, such as an MCP OAuth token, is refused the same way."""
    call = build_call(name)
    with person(app, auth_age=None) as client:
        assert_step_up_required(send(client, call))


@pytest.mark.parametrize("name", sorted(GATED))
def test_a_fresh_login_is_let_through(app, build_call, name):
    """A login inside the window reaches the route."""
    call = build_call(name)
    with person(app, auth_age=30) as client:
        assert_let_through(send(client, call))


@pytest.mark.parametrize("name", sorted(GATED))
def test_an_agent_key_is_let_through(app, build_call, name):
    """An agent key has no login to age, so only its scopes limit it."""
    call = build_call(name)
    if name == "mint an API key":
        pytest.skip("A key may never mint a key, which the API key tests assert.")
    with agent(app) as client:
        assert_let_through(send(client, call))


def test_a_refused_run_role_change_writes_nothing(app, auth_client, workspace):
    """A refused run role change leaves the stored role exactly as it was."""
    with person(app, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as client:
        assert_step_up_required(client.patch(_workspace_path(workspace), json={"run_role_arn": NEW_ROLE}))
    assert auth_client.get(_workspace_path(workspace)).json()["run_role_arn"] == workspace["run_role_arn"]


@pytest.mark.parametrize(
    "body",
    [
        {"description": "A plain edit."},
        {"run_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run"},
    ],
    ids=["an unrelated field", "the stored run role resent"],
)
def test_an_ordinary_workspace_edit_needs_no_step_up(app, workspace, body):
    """Only a change to the run role is gated, so a settings form resending it is not."""
    with person(app, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as client:
        response = client.patch(_workspace_path(workspace), json=body)
    assert response.status_code == 200, response.text


def test_a_plain_variable_needs_no_step_up(app, workspace):
    """Writing and deleting a variable that is not sensitive is not gated."""
    path = f"{_workspace_path(workspace)}/variables/region"
    with person(app, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as client:
        assert client.put(path, json={"value": "us-west-2"}).status_code == 200
        assert client.delete(path).status_code == 204


def test_a_stale_login_still_reads(app, workspace):
    """Reads are never gated."""
    with person(app, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as client:
        assert client.get(_workspace_path(workspace)).status_code == 200
        assert client.get("/api/v1/api-keys").status_code == 200
