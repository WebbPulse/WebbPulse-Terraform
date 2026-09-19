"""The `/api/auth` surface the workspaces function serves.

The bug these cover is the one that made staging unsignable-into: every `/api/auth`
route was declared in the gateway and pointed at the workspaces function, but the
backend mounted no identity router, so each one reached the app's 404 envelope.
Asserting the discovery document is what holds that mounting in place.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.domains.identity.conftest import ISSUER

DISCOVERY_PATH = "/api/auth/.well-known/openid-configuration"

JWKS_PATH = "/api/auth/.well-known/jwks.json"


def test_the_discovery_document_is_served(identity_client: TestClient) -> None:
    """The document exists and names this deployment's issuer.

    A 404 here is exactly the live failure: the routes declared but nothing mounted.
    """
    response = identity_client.get(DISCOVERY_PATH)
    assert response.status_code == 200, response.text
    assert response.json()["issuer"] == ISSUER


def test_the_discovery_document_points_at_the_served_jwks(identity_client: TestClient) -> None:
    """`jwks_uri` names a path this same application answers.

    An authorizer follows the document to find the keys, so a `jwks_uri` on a path
    the app does not serve verifies no token at all.
    """
    document = identity_client.get(DISCOVERY_PATH).json()
    assert document["jwks_uri"] == f"{ISSUER}/.well-known/jwks.json"
    assert identity_client.get(JWKS_PATH).status_code == 200


def test_the_jwks_carries_a_usable_signing_key(identity_client: TestClient) -> None:
    """The key set holds at least one RS256 signing key with a key id.

    `kid` is what an authorizer matches a token's header against, so a key set
    without one verifies nothing even when the key itself is right.
    """
    keys = identity_client.get(JWKS_PATH).json()["keys"]
    assert keys
    first = keys[0]
    assert first["alg"] == "RS256"
    assert first["use"] == "sig"
    assert first["kid"]


def test_the_login_route_is_mounted(identity_client: TestClient) -> None:
    """`POST /api/auth/login` is served rather than 404.

    Asserted by the response not being a 404: a rejected body is a mounted route
    refusing bad input, which is what distinguishes it from the route being absent.
    """
    response = identity_client.post("/api/auth/login", json={})
    assert response.status_code != 404


def test_the_identity_routes_are_absent_without_an_issuer(app, settings) -> None:
    """With no `IDENTITY_ISSUER` nothing under `/api/auth` mounts.

    The `app` fixture runs without the identity environment, which is the state a
    deployment before the identity module is in place has. The glue must then build
    none of its clients rather than fail at import.
    """
    del settings
    with TestClient(app) as client:
        assert client.get(DISCOVERY_PATH).status_code == 404


def test_the_product_routes_still_mount_alongside_identity(identity_client: TestClient) -> None:
    """Adding the identity router does not displace the workspaces routes.

    Both surfaces are served by the one function, so a glue that mounted at the
    wrong prefix could shadow `/api/v1`. A 401 is the guard answering, which means
    the route is there.
    """
    assert identity_client.get("/api/v1/workspaces").status_code == 401


def test_the_registration_route_is_not_declared(identity_client: TestClient) -> None:
    """Registration is off, so the package declares no register route.

    The deployed environments set `IDENTITY_REGISTRATION_ENABLED` to `false`, and
    the first account is created by `scripts/create_user.py` instead. The package
    declares the route and refuses it rather than omitting it, so the assertion is
    that no body reaches a create: a 403 whatever is posted.
    """
    assert identity_client.post("/api/auth/register", json={}).status_code == 403
    refused = identity_client.post(
        "/api/auth/register",
        json={"email": "intruder@example.test", "password": "a-sufficiently-long-password"},
    )
    assert refused.status_code == 403
