"""The agent API key routes: mint, list, revoke, and who may do each.

A human caller is stood up by sending the request context header the Lambda Web
Adapter injects, carrying the claims the gateway's JWT authorizer produced. That
is the only way to exercise the distinction these routes turn on: the rest of the
suite authenticates with real `wpk_` keys, and a key is exactly the actor the
mint route has to refuse.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from webbpulse.http import REQUEST_CONTEXT_HEADER

from app.common.core.auth import (
    ALL_SCOPES,
    RUNNER_SCOPE,
    RUNS_READ,
    RUNS_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
)
from app.domains.workspaces import api_keys_service as service
from app.domains.workspaces.api_keys_router import (
    KEY_ACTOR_CODE,
    KEY_LIMIT_CODE,
    SCOPES_EXCEEDED_CODE,
)

USER_ID = "user-human"
OTHER_USER_ID = "user-other"


def request_context(
    *,
    user_id: str = USER_ID,
    scopes: tuple[str, ...] = ALL_SCOPES,
    roles: tuple[str, ...] = (),
) -> dict[str, str]:
    """The header a route behind the JWT authorizer sees, carrying verified claims.

    The gateway flattens every claim to a string, so `roles` goes down as the
    bracketed form and `scope` as the space-joined one, which is what
    `coerce_claims` expects to parse back.
    """
    return {
        REQUEST_CONTEXT_HEADER: json.dumps(
            {
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": user_id,
                            "scope": " ".join(scopes),
                            "roles": json.dumps(list(roles)),
                        }
                    }
                }
            }
        )
    }


@pytest.fixture
def human_client(app):
    """A client authenticated as a signed-in person holding every scope."""
    with TestClient(app, headers=request_context()) as client:
        yield client


@pytest.fixture
def human(app):
    """A factory building a client for a person with given scopes and roles."""

    def build(
        *,
        user_id: str = USER_ID,
        scopes: tuple[str, ...] = ALL_SCOPES,
        roles: tuple[str, ...] = (),
    ) -> TestClient:
        """A client presenting the claims a JWT for that person would carry."""
        return TestClient(app, headers=request_context(user_id=user_id, scopes=scopes, roles=roles))

    return build


def mint_via_api(client: TestClient, **body: Any) -> dict[str, Any]:
    """Mint a key through the route and return the created body."""
    payload = {"name": "an-agent", **body}
    response = client.post("/api/v1/api-keys", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_minting_returns_the_plaintext_once(human_client):
    """The create response carries the key, and nothing else ever does."""
    created = mint_via_api(human_client)

    assert created["key"].startswith("wpk_")
    assert created["key_id"]
    assert created["prefix"] == created["key"][:12]

    listed = human_client.get("/api/v1/api-keys")
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    assert "key" not in items[0]
    assert items[0]["key_id"] == created["key_id"]


def test_a_minted_key_authenticates(app, human_client):
    """A key from the route works as a credential on a guarded route.

    The point of the whole feature: the plaintext the response hands back is a
    bearer the rest of the surface accepts.
    """
    created = mint_via_api(human_client, scopes=[WORKSPACES_READ])

    with TestClient(app, headers={"Authorization": f"Bearer {created['key']}"}) as agent:
        assert agent.get("/api/v1/workspaces").status_code == 200
        assert agent.post("/api/v1/workspaces", json={"name": "x", "engine_version": "1.11.4"}).status_code == 403


def test_a_key_defaults_to_the_callers_whole_scope_set(human):
    """Omitting `scopes` mints a key carrying everything the caller holds."""
    with human(scopes=(WORKSPACES_READ, RUNS_READ)) as caller:
        created = mint_via_api(caller)

    assert created["scopes"] == [RUNS_READ, WORKSPACES_READ]


def test_a_key_can_be_narrowed_below_the_caller(human):
    """A caller may mint a key carrying less than they hold."""
    with human(scopes=(WORKSPACES_READ, WORKSPACES_WRITE, RUNS_READ)) as caller:
        created = mint_via_api(caller, scopes=[WORKSPACES_READ])

    assert created["scopes"] == [WORKSPACES_READ]


def test_a_key_cannot_be_widened_above_the_caller(human):
    """Asking for a scope the caller does not hold is a 403, not a silent narrowing.

    Refused rather than intersected: a key quietly minted narrower than asked for
    fails later, somewhere else, in a way nobody connects back to the mint.
    """
    with human(scopes=(WORKSPACES_READ,)) as caller:
        response = caller.post("/api/v1/api-keys", json={"name": "greedy", "scopes": [WORKSPACES_WRITE]})

    assert response.status_code == 403
    assert response.json()["error_code"] == SCOPES_EXCEEDED_CODE


def test_a_key_cannot_carry_the_runner_scope(human):
    """`runner` never reaches a key minted here, even from claims carrying it.

    A key holding it would open every run's bundle, which carries decrypted
    variables, so it is stripped from the caller's set rather than trusted.
    """
    with human(scopes=(WORKSPACES_READ, RUNNER_SCOPE)) as caller:
        created = mint_via_api(caller)
        response = caller.post("/api/v1/api-keys", json={"name": "runner", "scopes": [RUNNER_SCOPE]})

    assert RUNNER_SCOPE not in created["scopes"]
    assert response.status_code == 403


def test_an_api_key_cannot_mint_another_key(app, human_client):
    """A key presenting itself at the mint route is refused with its own code.

    Its own code rather than `INSUFFICIENT_SCOPE`, because no scope fixes it: the
    refusal is about which credential arrived.
    """
    created = mint_via_api(human_client)

    with TestClient(app, headers={"Authorization": f"Bearer {created['key']}"}) as agent:
        response = agent.post("/api/v1/api-keys", json={"name": "successor"})

    assert response.status_code == 403
    assert response.json()["error_code"] == KEY_ACTOR_CODE


def test_an_api_key_may_still_list_and_revoke(app, human_client):
    """A key sees and can retire its own owner's keys.

    Only minting is refused to a key. Listing and revoking are how a rotating
    service retires the key it is holding, which it has to be able to do with the
    only credential it has.
    """
    first = mint_via_api(human_client, name="first")
    second = mint_via_api(human_client, name="second")

    with TestClient(app, headers={"Authorization": f"Bearer {first['key']}"}) as agent:
        listed = agent.get("/api/v1/api-keys")
        assert listed.status_code == 200, listed.text
        assert {item["key_id"] for item in listed.json()["items"]} == {first["key_id"], second["key_id"]}

        revoked = agent.delete(f"/api/v1/api-keys/{second['key_id']}")
        assert revoked.status_code == 200, revoked.text


def test_listing_shows_only_the_callers_own_keys(human):
    """One person's list never carries another person's key."""
    with human(user_id=USER_ID) as mine:
        mint_via_api(mine, name="mine")
    with human(user_id=OTHER_USER_ID) as theirs:
        mint_via_api(theirs, name="theirs")
        listed = theirs.get("/api/v1/api-keys").json()["items"]

    assert [item["name"] for item in listed] == ["theirs"]


def test_listing_leaves_out_run_tokens(human_client, created_run):
    """A run token lives in the same table and never appears in anybody's list."""
    del created_run
    listed = human_client.get("/api/v1/api-keys").json()["items"]

    assert not [item for item in listed if RUNNER_SCOPE in item["scopes"]]


def test_revoking_stops_the_key_working(app, human_client):
    """A revoked key is refused on its next request, and reads as revoked in the list."""
    created = mint_via_api(human_client)

    with TestClient(app, headers={"Authorization": f"Bearer {created['key']}"}) as agent:
        assert agent.get("/api/v1/workspaces").status_code == 200

    response = human_client.delete(f"/api/v1/api-keys/{created['key_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["revoked_at"]

    with TestClient(app, headers={"Authorization": f"Bearer {created['key']}"}) as agent:
        assert agent.get("/api/v1/workspaces").status_code == 401


def test_revoking_twice_is_a_404(human_client):
    """A key already revoked has nothing live to retire."""
    created = mint_via_api(human_client)

    assert human_client.delete(f"/api/v1/api-keys/{created['key_id']}").status_code == 200
    assert human_client.delete(f"/api/v1/api-keys/{created['key_id']}").status_code == 404


def test_revoking_somebody_elses_key_is_a_404(human):
    """Another person's key reads as absent, not as forbidden.

    A 403 would confirm the id exists, which turns a key id into a way to
    enumerate other people's credentials.
    """
    with human(user_id=USER_ID) as mine:
        created = mint_via_api(mine)
    with human(user_id=OTHER_USER_ID) as theirs:
        response = theirs.delete(f"/api/v1/api-keys/{created['key_id']}")

    assert response.status_code == 404


def test_an_admin_revokes_anybodys_key(human):
    """An admin retires a key whose owner has lost their access."""
    with human(user_id=USER_ID) as mine:
        created = mint_via_api(mine)
    with human(user_id=OTHER_USER_ID, roles=("admin",)) as admin:
        response = admin.delete(f"/api/v1/api-keys/{created['key_id']}")

    assert response.status_code == 200, response.text
    assert response.json()["revoked_at"]


def test_a_run_token_cannot_be_revoked_through_the_route(human, created_run):
    """Revoking a run token here would strand a run holding a lease on real infrastructure."""
    from webbpulse.identity.api_keys import hash_key

    key_id = hash_key(created_run["run_token"])
    with human(roles=("admin",)) as admin:
        response = admin.delete(f"/api/v1/api-keys/{key_id}")

    assert response.status_code == 404


def test_an_expiry_is_stored_and_a_past_one_refused(human_client):
    """A future expiry lands on the record; a past one is a 422."""
    future = datetime.now(UTC) + timedelta(days=30)
    created = mint_via_api(human_client, expires_at=future.isoformat())
    assert created["expires_at"] == int(future.timestamp())

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    response = human_client.post("/api/v1/api-keys", json={"name": "stale", "expires_at": past})
    assert response.status_code == 422


def test_an_expiry_beyond_the_ceiling_is_refused(human_client):
    """A key may not be minted to outlive `MAX_EXPIRY_YEARS`."""
    far = (datetime.now(UTC) + timedelta(days=365 * service.MAX_EXPIRY_YEARS + 30)).isoformat()
    response = human_client.post("/api/v1/api-keys", json={"name": "forever", "expires_at": far})

    assert response.status_code == 422


def test_the_key_limit_is_enforced(human_client, monkeypatch):
    """A caller at the limit gets a 409 rather than an unbounded table."""
    monkeypatch.setattr(service, "MAX_KEYS_PER_USER", 2)
    mint_via_api(human_client, name="one")
    mint_via_api(human_client, name="two")

    response = human_client.post("/api/v1/api-keys", json={"name": "three"})
    assert response.status_code == 409
    assert response.json()["error_code"] == KEY_LIMIT_CODE


def test_a_revoked_key_frees_a_slot(human_client, monkeypatch):
    """The limit counts live keys, so retiring one makes room for another."""
    monkeypatch.setattr(service, "MAX_KEYS_PER_USER", 1)
    created = mint_via_api(human_client, name="one")
    assert human_client.post("/api/v1/api-keys", json={"name": "two"}).status_code == 409

    assert human_client.delete(f"/api/v1/api-keys/{created['key_id']}").status_code == 200
    mint_via_api(human_client, name="two")


def test_every_api_key_route_refuses_an_anonymous_caller(client):
    """None of the three answers without a credential."""
    assert client.get("/api/v1/api-keys").status_code == 401
    assert client.post("/api/v1/api-keys", json={"name": "x"}).status_code == 401
    assert client.delete(f"/api/v1/api-keys/{'a' * 64}").status_code == 401


def test_narrowed_scopes_refuses_what_is_not_held():
    """The narrowing rule, asserted directly rather than only through a route."""
    assert service.narrowed_scopes(None, (WORKSPACES_READ, RUNS_READ)) == (RUNS_READ, WORKSPACES_READ)
    assert service.narrowed_scopes([], (WORKSPACES_READ,)) == (WORKSPACES_READ,)
    assert service.narrowed_scopes([WORKSPACES_READ], (WORKSPACES_READ, RUNS_WRITE)) == (WORKSPACES_READ,)

    with pytest.raises(service.ScopesExceeded) as raised:
        service.narrowed_scopes([RUNS_WRITE], (WORKSPACES_READ,))
    assert raised.value.excess == (RUNS_WRITE,)
