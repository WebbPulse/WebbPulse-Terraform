"""`terraform login`: the SPA approval and the CLI's token exchange, end to end in process."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from fastapi.testclient import TestClient
from webbpulse.audit import AuditQuery
from webbpulse.identity.oauth import pkce_challenge
from webbpulse.identity.oauth_server_storage import AUTHORIZATION_CODES_TABLE, OAUTH_SERVER_TABLES

from app.common import audit
from app.common.core.auth import (
    ALL_SCOPES,
    REGISTRY_READ,
    RUNS_APPLY,
    STATE_DOWNLOAD,
    STATE_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
)
from app.domains.workspaces import terraform_login
from app.domains.workspaces.api_keys_router import KEY_ACTOR_CODE
from app.domains.workspaces.terraform_login_router import LOGIN_REFUSED_CODE
from tests.conftest import TABLE_PREFIX, mint_key, person_headers, seed_user

USER_ID = "user-login"
VERIFIER = "v" * 43 + "erifier-for-terraform-login"
REDIRECT = "http://localhost:10000/login"


@pytest.fixture(autouse=True)
def codes_table() -> None:
    """The identity module's `authorization-codes` table, from the package's own spec."""
    spec = next(spec for spec in OAUTH_SERVER_TABLES if spec.logical_name == AUTHORIZATION_CODES_TABLE)
    boto3.client("dynamodb", region_name="us-west-2").create_table(**spec.create_table_request(TABLE_PREFIX))


def request_body(**overrides: str) -> dict[str, str]:
    """The query Terraform CLI opens the approve page with."""
    return {
        "client_id": "terraform-cli",
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "code_challenge": pkce_challenge(VERIFIER),
        "code_challenge_method": "S256",
        "state": "state-123",
        **overrides,
    }


def person(app: Any, *, scopes: tuple[str, ...] = ALL_SCOPES, auth_age: int | None = 0) -> TestClient:
    """A client for a signed-in person."""
    seed_user(USER_ID)
    return TestClient(app, headers=person_headers(user_id=USER_ID, scopes=scopes, auth_age=auth_age))


def approve(app: Any, **overrides: str) -> str:
    """Approve a login and return the code from the redirect."""
    response = person(app).post("/api/v1/oauth/authorizations", json=request_body(**overrides))
    assert response.status_code == 201, response.text
    query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
    assert query["state"] == ["state-123"]
    return query["code"][0]


def exchange(client: TestClient, code: str, **overrides: str) -> Any:
    """Post the token request the way Terraform CLI does, form encoded."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": "terraform-cli",
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        **overrides,
    }
    return client.post("/v1/oauth/token", data={key: value for key, value in form.items() if value is not None})


def test_login_mints_a_scoped_ninety_day_key(app, client):
    """The whole flow: approve, exchange, and the key works against the API."""
    code = approve(app)
    response = exchange(client, code)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"].startswith("wpk_")

    keys = person(app).get("/api/v1/api-keys").json()["items"]
    login = next(key for key in keys if key["name"] == terraform_login.KEY_NAME)
    assert WORKSPACES_READ in login["scopes"]
    assert REGISTRY_READ in login["scopes"]
    assert RUNS_APPLY in login["scopes"]
    assert STATE_DOWNLOAD in login["scopes"]
    assert STATE_WRITE in login["scopes"]
    assert WORKSPACES_WRITE not in login["scopes"]

    listed = client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert listed.status_code == 200


def test_the_login_key_mint_is_audited_as_the_person_on_the_cli(app, client):
    """The exchange records the key's creation against the approving person, never its plaintext."""
    response = exchange(client, approve(app))
    assert response.status_code == 200, response.text

    events = audit.store().list_events(audit.AUDIT_TENANT, AuditQuery(action=audit.API_KEY_CREATED), limit=10).events
    [event] = [event for event in events if event.target.label == terraform_login.KEY_NAME]
    assert event.actor.id == USER_ID
    assert event.actor.kind == "user"
    assert event.actor.source == "cli"
    assert RUNS_APPLY in event.payload["scopes"]
    assert response.json()["access_token"] not in repr(event)


def test_the_client_id_may_arrive_as_basic_auth(app, client):
    """Go's oauth2 tries HTTP Basic before the body."""
    code = approve(app)
    basic = base64.b64encode(b"terraform-cli:").decode()
    response = client.post(
        "/v1/oauth/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": VERIFIER},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert response.status_code == 200, response.text


def test_a_code_is_spent_once(app, client):
    """A second exchange of the same code fails."""
    code = approve(app)
    assert exchange(client, code).status_code == 200
    again = exchange(client, code)
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_a_wrong_verifier_spends_the_code(app, client):
    """A mismatched verifier is refused, and the code cannot be retried."""
    code = approve(app)
    wrong = exchange(client, code, code_verifier="w" * 50)
    assert wrong.json()["error"] == "invalid_grant"
    assert exchange(client, code).json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"redirect_uri": "http://localhost:10001/login"}, "invalid_grant"),
        ({"client_id": "someone-else"}, "invalid_client"),
        ({"grant_type": "refresh_token"}, "unsupported_grant_type"),
        ({"code_verifier": "short"}, "invalid_request"),
    ],
)
def test_the_exchange_checks_every_bound_value(app, client, override, error):
    """The redirect, client, grant and verifier all have to match the approval."""
    code = approve(app)
    response = exchange(client, code, **override)
    assert response.json()["error"] == error
    assert response.status_code == (401 if error == "invalid_client" else 400)


@pytest.mark.parametrize(
    "override",
    [
        {"client_id": "other"},
        {"response_type": "token"},
        {"code_challenge_method": "plain"},
        {"code_challenge": "short"},
        {"redirect_uri": "http://localhost:9999/login"},
        {"redirect_uri": "http://localhost:10011/login"},
        {"redirect_uri": "https://localhost:10000/login"},
        {"redirect_uri": "http://evil.example:10000/login"},
        {"redirect_uri": "http://localhost:10000/other"},
        {"redirect_uri": "http://localhost:10000/login#x"},
    ],
)
def test_approval_refuses_anything_but_terraform(app, override):
    """Only Terraform CLI's client, PKCE S256 and its loopback listener are approved."""
    response = person(app).post("/api/v1/oauth/authorizations", json=request_body(**override))
    assert response.status_code == 422
    assert response.json()["error_code"] == LOGIN_REFUSED_CODE


def test_approval_accepts_the_address_form_and_every_port(app):
    """`127.0.0.1` and each port in the discovery range are Terraform's listener."""
    for port in (10000, 10010):
        approve(app, redirect_uri=f"http://127.0.0.1:{port}/login")


def test_approval_needs_a_recent_login(app):
    """Approving creates a key, so it sits behind the step-up gate."""
    response = person(app, auth_age=3600).post("/api/v1/oauth/authorizations", json=request_body())
    assert response.status_code == 401
    assert response.json()["error_code"] == "STEP_UP_REQUIRED"


def test_a_key_cannot_approve(app):
    """A key approving a login would mint a key, which only a person may do."""
    key = mint_key(*ALL_SCOPES, user_id=USER_ID)
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        response = client.post("/api/v1/oauth/authorizations", json=request_body())
    assert response.status_code == 403
    assert response.json()["error_code"] == KEY_ACTOR_CODE


def test_the_key_carries_only_scopes_the_person_holds(app, client):
    """A read-only person gets a read-only login key."""
    seed_user(USER_ID)
    with TestClient(app, headers=person_headers(user_id=USER_ID, scopes=(WORKSPACES_READ,))) as reader:
        response = reader.post("/api/v1/oauth/authorizations", json=request_body())
    assert response.status_code == 201
    assert response.json()["scopes"] == [WORKSPACES_READ]


def test_a_person_with_no_login_scope_is_refused(app):
    """Nothing to carry means no key."""
    seed_user(USER_ID)
    with TestClient(app, headers=person_headers(user_id=USER_ID, scopes=("registry:write",))) as writer:
        response = writer.post("/api/v1/oauth/authorizations", json=request_body())
    assert response.status_code == 422


def test_an_unknown_code_and_a_malformed_body_are_refused(client):
    """Garbage reaches the OAuth error shape, never a 500."""
    assert exchange(client, "nope").json()["error"] == "invalid_grant"
    response = client.post(
        "/v1/oauth/token", content=b"\xff\xfe", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 400
    assert client.post("/v1/oauth/token", content=b"a" * 9000).json()["error"] == "invalid_request"


def test_a_login_key_applies_on_an_old_login(app, client, awaiting_confirmation):
    """`terraform login` leaves a key that confirms an apply whenever, as an HCP user token does."""
    seed_user(USER_ID)
    with TestClient(app, headers=person_headers(user_id=USER_ID, auth_age=0)) as fresh:
        response = fresh.post("/api/v1/oauth/authorizations", json=request_body())
    code = parse_qs(urlsplit(response.json()["redirect_url"]).query)["code"][0]
    token = exchange(client, code).json()["access_token"]

    run_id = awaiting_confirmation["run_id"]
    confirmed = client.post(f"/api/v1/runs/{run_id}/confirm", headers={"Authorization": f"Bearer {token}"})
    assert confirmed.status_code == 200, confirmed.text
