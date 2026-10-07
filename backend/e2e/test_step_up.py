"""Step-up re-authentication against the deployed stage.

Sensitive writes need a login inside the last fifteen minutes. A stepped up session is let
through, an agent key is exempt, and a token whose `auth_time` is old or missing is refused
with `STEP_UP_REQUIRED` and the RFC 9470 challenge, which a step-up clears so the same call
can be replayed. Confirming and discarding a run are not gated: a stale login with
`runs:apply` reaches the run. The cases that mint their own token run only where
`E2E_MINT_ENABLED` is set.

Nothing here prints a password, a token or a minted key.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

STEP_UP_REQUIRED = "STEP_UP_REQUIRED"
MAX_AGE_SECONDS = 15 * 60
API_KEYS = "/api/v1/api-keys"
MISSING_KEY_ID = "0" * 64
MISSING_RUN_ID = f"run-{'0' * 26}"

pytestmark = pytest.mark.e2e_writes


def _assert_step_up_required(response: Any) -> None:
    """The refusal is the application's own step-up challenge, not a gateway 401."""
    assert response.status_code == 401, f"answered {response.status_code}"
    body = response.json()
    assert body.get("error_code") == STEP_UP_REQUIRED, f"refused with {body.get('error_code')}"
    assert body.get("max_age") == MAX_AGE_SECONDS
    challenge = response.headers.get("WWW-Authenticate", "")
    assert 'error="insufficient_user_authentication"' in challenge
    assert f"max_age={MAX_AGE_SECONDS}" in challenge


def test_a_stepped_up_session_mints_and_revokes_a_key(stepped_up_session: Any, e2e_env: Any) -> None:
    """Minting and revoking a key, both gated, go through straight after a step-up."""
    auth_time = int(stepped_up_session.claims.get("auth_time") or 0)
    assert time.time() - auth_time < MAX_AGE_SECONDS, "the stepped up token carries no recent auth_time"

    api = stepped_up_session.client
    minted = api.post(API_KEYS, json={"name": f"{e2e_env.resource_prefix}step-up", "scopes": ["workspaces:read"]})
    assert minted.status_code == 201, f"minting answered {minted.status_code}: {minted.text[:200]}"
    key_id = minted.json()["key_id"]

    revoked = api.delete(f"{API_KEYS}/{key_id}")
    assert revoked.status_code == 200, f"revoking answered {revoked.status_code}"
    assert revoked.json().get("revoked_at")


def test_an_agent_key_needs_no_step_up(stepped_up_session: Any, e2e_env: Any) -> None:
    """A `wpk_` key has no login to age, so a gated route answers it on its scopes alone."""
    api = stepped_up_session.client
    minted = api.post(
        API_KEYS, json={"name": f"{e2e_env.resource_prefix}step-up-agent", "scopes": ["workspaces:write"]}
    )
    assert minted.status_code == 201, f"minting answered {minted.status_code}"
    body = minted.json()
    try:
        agent = api.with_token(body["key"])
        response = agent.delete(f"/api/v1/workspaces/ws-{'0' * 26}")
        assert response.status_code == 404, f"the agent key answered {response.status_code}"
    finally:
        api.delete(f"{API_KEYS}/{body['key_id']}")


def test_a_stale_login_is_refused(api: Any, minted_token: Any) -> None:
    """A token whose login is older than the window must step up before revoking a key."""
    stale = minted_token({"auth_time": int(time.time()) - MAX_AGE_SECONDS - 300})
    _assert_step_up_required(api.with_token(stale).delete(f"{API_KEYS}/{MISSING_KEY_ID}"))


def test_a_token_without_auth_time_is_refused(api: Any, minted_token: Any) -> None:
    """A token that names no login time, as an MCP OAuth token does, is refused the same way."""
    _assert_step_up_required(api.with_token(minted_token()).delete(f"{API_KEYS}/{MISSING_KEY_ID}"))


@pytest.mark.parametrize("auth_time_age", [MAX_AGE_SECONDS + 300, None], ids=["stale", "undated"])
def test_a_stale_login_confirms_and_discards_without_a_step_up(
    api: Any, minted_token: Any, auth_time_age: int | None
) -> None:
    """Confirming or discarding a run passes the auth checks on an old login and reaches the run lookup."""
    claims = {} if auth_time_age is None else {"auth_time": int(time.time()) - auth_time_age}
    stale = api.with_token(minted_token(claims))
    for action in ("confirm", "discard"):
        response = stale.post(f"/api/v1/runs/{MISSING_RUN_ID}/{action}")
        assert response.status_code == 404, f"{action} answered {response.status_code}: {response.text[:200]}"


def test_a_refused_call_goes_through_once_the_login_steps_up(
    api: Any, minted_token: Any, user_session: Any, credentials: Any
) -> None:
    """The browser's retry path: a stale login is refused, steps up in place, and the replay is let through."""
    from webbpulse.e2e.identity import step_up

    stale = minted_token({"auth_time": int(time.time()) - MAX_AGE_SECONDS - 300})
    _assert_step_up_required(api.with_token(stale).delete(f"{API_KEYS}/{MISSING_KEY_ID}"))

    replayed = step_up(user_session, credentials.password).client.delete(f"{API_KEYS}/{MISSING_KEY_ID}")
    assert replayed.status_code == 404, f"the replay answered {replayed.status_code}"
