"""The workspace setting that grants its runs a control plane API token."""

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import ADMIN, ALL_SCOPES, STEP_UP_MAX_AGE_SECONDS
from tests.conftest import person_headers, seed_user

PERSON = "user-grant"


def _path(workspace):
    """The workspace's API path."""
    return f"/api/v1/workspaces/{workspace['workspace_id']}"


def _person(app, *, admin: bool, auth_age: int = 0) -> TestClient:
    """A person who signed in `auth_age` seconds ago, holding every scope but admin unless `admin`."""
    seed_user(PERSON, is_admin=admin)
    scopes = ALL_SCOPES if admin else tuple(scope for scope in ALL_SCOPES if scope != ADMIN)
    roles = ("admin",) if admin else ()
    return TestClient(app, headers=person_headers(user_id=PERSON, scopes=scopes, roles=roles, auth_age=auth_age))


def test_a_workspace_grants_nothing_by_default(workspace):
    """Runs get no API token until an admin opts the workspace in."""
    assert workspace["run_api_token_scopes"] == []


def test_an_admin_sets_the_grant_in_canonical_order(app, workspace):
    """Duplicates go and the order is fixed, so resending it reordered is no change."""
    with _person(app, admin=True) as client:
        response = client.patch(
            _path(workspace),
            json={"run_api_token_scopes": ["registry:read", "workspaces:read", "registry:read"]},
        )

    assert response.status_code == 200, response.text
    assert response.json()["run_api_token_scopes"] == ["workspaces:read", "registry:read"]


def test_a_non_admin_cannot_set_the_grant(app, workspace):
    """Workspace write is not enough to hand runs a key."""
    with _person(app, admin=False) as client:
        response = client.patch(_path(workspace), json={"run_api_token_scopes": ["workspaces:read"]})

    assert response.status_code == 403, response.text


@pytest.mark.parametrize("stored", [[], ["registry:read"]], ids=["no grant", "a grant"])
def test_a_refused_non_admin_change_leaves_the_stored_grant(app, auth_client, workspace, stored):
    """The 403 writes nothing, whether a grant was stored or not."""
    auth_client.patch(_path(workspace), json={"run_api_token_scopes": stored})
    six = [
        "workspaces:read",
        "workspaces:write",
        "variables:read",
        "variables:write",
        "registry:read",
        "registry:write",
    ]

    with _person(app, admin=False) as client:
        response = client.patch(_path(workspace), json={"run_api_token_scopes": six})

    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == "INSUFFICIENT_SCOPE"
    assert auth_client.get(_path(workspace)).json()["run_api_token_scopes"] == stored


def test_an_admin_with_a_stale_login_must_step_up(app, auth_client, workspace):
    """Admin alone is not enough: a login older than the window gets the step-up error and writes nothing."""
    with _person(app, admin=True, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as client:
        response = client.patch(_path(workspace), json={"run_api_token_scopes": ["workspaces:read"]})

    assert response.status_code == 401, response.text
    assert response.json()["error_code"] == "STEP_UP_REQUIRED"
    assert auth_client.get(_path(workspace)).json()["run_api_token_scopes"] == []


def test_a_patch_response_reports_an_empty_grant_as_an_empty_list(app, workspace):
    """An edit to another field echoes the stored empty grant as `[]`, never null."""
    with _person(app, admin=False) as client:
        response = client.patch(_path(workspace), json={"description": "edited"})

    assert response.status_code == 200, response.text
    assert response.json()["run_api_token_scopes"] == []


def test_an_unknown_field_is_refused_rather_than_dropped(auth_client, workspace):
    """A key the server cannot store is a 422, so it never reads back as a stored change."""
    response = auth_client.patch(_path(workspace), json={"run_api_token_scope": ["workspaces:read"]})

    assert response.status_code == 422, response.text
    assert auth_client.get(_path(workspace)).json()["run_api_token_scopes"] == []


def test_a_non_admin_key_cannot_set_the_grant(scoped_client, workspace):
    """An agent key without admin is refused the same way."""
    with scoped_client("workspaces:read", "workspaces:write") as client:
        response = client.patch(_path(workspace), json={"run_api_token_scopes": ["workspaces:read"]})

    assert response.status_code == 403, response.text


def test_a_non_admin_may_resend_the_stored_grant(app, auth_client, workspace):
    """Echoing the stored value back is not a change, so a full form save still works."""
    auth_client.patch(_path(workspace), json={"run_api_token_scopes": ["workspaces:read"]})

    with _person(app, admin=False) as client:
        response = client.patch(
            _path(workspace), json={"run_api_token_scopes": ["workspaces:read"], "description": "edited"}
        )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("scope", ["admin", "runner", "runner:registry", "runs:apply", "runs:write", "state:download"])
def test_only_the_six_run_api_scopes_can_be_granted(auth_client, workspace, scope):
    """Never admin, runner, apply or state."""
    response = auth_client.patch(_path(workspace), json={"run_api_token_scopes": [scope]})

    assert response.status_code == 422, response.text


@pytest.mark.parametrize("cleared", [None, []])
def test_null_or_empty_clears_the_grant(auth_client, workspace, cleared):
    """Both read back as no grant."""
    auth_client.patch(_path(workspace), json={"run_api_token_scopes": ["workspaces:read"]})

    response = auth_client.patch(_path(workspace), json={"run_api_token_scopes": cleared})

    assert response.status_code == 200, response.text
    assert response.json()["run_api_token_scopes"] == []


def test_the_grant_cannot_be_set_at_create(auth_client):
    """Opting in is an edit, so it always passes the admin gate."""
    from tests.conftest import WORKSPACE_PAYLOAD

    response = auth_client.post(
        "/api/v1/workspaces",
        json={**WORKSPACE_PAYLOAD, "name": "granted-at-create", "run_api_token_scopes": ["workspaces:read"]},
    )

    assert response.status_code in (201, 422)
    if response.status_code == 201:
        assert response.json()["run_api_token_scopes"] == []
