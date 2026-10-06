"""The workspace setting that grants its runs a control plane API token."""

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import ADMIN, ALL_SCOPES
from tests.conftest import person_headers, seed_user

PERSON = "user-grant"


def _path(workspace):
    """The workspace's API path."""
    return f"/api/v1/workspaces/{workspace['workspace_id']}"


def _person(app, *, admin: bool) -> TestClient:
    """A person who just signed in, holding every scope but admin unless `admin`."""
    seed_user(PERSON, is_admin=admin)
    scopes = ALL_SCOPES if admin else tuple(scope for scope in ALL_SCOPES if scope != ADMIN)
    roles = ("admin",) if admin else ()
    return TestClient(app, headers=person_headers(user_id=PERSON, scopes=scopes, roles=roles, auth_age=0))


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
