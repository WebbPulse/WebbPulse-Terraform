"""`terraform login` against the deployed staging stage, driven headlessly.

Terraform CLI reads `login.v1` from the SPA host's discovery document, opens the approve
page with its PKCE challenge and a loopback redirect, and exchanges the code the page hands
back at the token endpoint. These cases play both halves: the run's signed-in user approves
through `POST /api/v1/oauth/authorizations` (step-up gated, as the approve page is), and the
exchange posts the form Terraform's oauth2 client sends, with plain `httpx` and no gate
header, exactly as the CLI does. The minted `terraform login` keys are revoked on teardown,
whatever the outcome. Skipped outside staging and on the read-only production smoke, since
a key is minted. No key, code or verifier is ever printed.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

REGISTRY_HOST = "staging.terraform.webbpulse.com"
API_HOST = "api.staging.terraform.webbpulse.com"
TOKEN_URL = f"https://{API_HOST}/v1/oauth/token"
REDIRECT = "http://localhost:10000/login"
CLIENT_ID = "terraform-cli"
KEY_NAME = "terraform login"
PROOF_MODULE = f"https://{API_HOST}/v1/modules/WebbPulse/registry-proof/null/versions"
TIMEOUT_SECONDS = 30

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.xdist_group("terraform-login"),
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="minting a login key is a write, and the registry proof module is on staging only",
    ),
]


def _challenge(verifier: str) -> str:
    """The S256 PKCE challenge Terraform CLI derives from its verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _exchange(code: str, verifier: str) -> httpx.Response:
    """The token request as Terraform CLI's oauth2 client sends it, form encoded."""
    return httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
        timeout=TIMEOUT_SECONDS,
    )


@pytest.fixture
def approve(step_up_again: Callable[[], Any]) -> Iterator[Callable[[str], str]]:
    """Approve logins as the run's user, revoking every `terraform login` key on teardown.

    Approving and revoking are step-up gated, so each steps up right before it.
    """

    def _approve(verifier: str) -> str:
        """Approve one login for `verifier` and return the code from the loopback redirect."""
        state = secrets.token_urlsafe(16)
        response = step_up_again().post(
            "/api/v1/oauth/authorizations",
            json={
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "state": state,
            },
        )
        assert response.status_code == 201, f"approving answered {response.status_code}: {response.text[:400]}"
        redirect = urlsplit(response.json()["redirect_url"])
        assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == REDIRECT
        query = parse_qs(redirect.query)
        assert query.get("state") == [state], "the redirect did not carry the request's state"
        return query["code"][0]

    yield _approve

    api = step_up_again()
    listed = api.get("/api/v1/api-keys")
    assert listed.status_code == 200, f"listing keys answered {listed.status_code}"
    failures = []
    for key in listed.json()["items"]:
        if key["name"] != KEY_NAME or key.get("revoked_at"):
            continue
        response = api.delete(f"/api/v1/api-keys/{key['key_id']}")
        if response.status_code != 200:
            failures.append(f"{key['key_id']} ({response.status_code})")
    if failures:
        pytest.fail(f"e2e teardown could not revoke the login keys: {', '.join(failures)}")


def test_discovery_advertises_login_and_providers() -> None:
    """The SPA host's discovery document carries what `terraform login` and provider installs read."""
    response = httpx.get(f"https://{REGISTRY_HOST}/.well-known/terraform.json", timeout=TIMEOUT_SECONDS)
    assert response.status_code == 200
    body = response.json()
    assert body.get("providers.v1") == f"https://{API_HOST}/v1/providers/"
    login = body.get("login.v1")
    assert login == {
        "client": CLIENT_ID,
        "grant_types": ["authz_code"],
        "authz": "/oauth/authorize",
        "token": TOKEN_URL,
        "ports": [10000, 10010],
    }


def test_login_issues_a_key_that_reads_the_registry(approve: Callable[[str], str]) -> None:
    """Approve, exchange, and use the key as `TF_TOKEN_<host>` would against the registry."""
    verifier = secrets.token_urlsafe(48)
    code = approve(verifier)
    response = _exchange(code, verifier)
    assert response.status_code == 200, f"the exchange answered {response.status_code}: {response.text[:200]}"
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    token = body["access_token"]
    assert token.startswith("wpk_"), "the login token is not a wpk_ key"

    versions = httpx.get(PROOF_MODULE, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_SECONDS)
    assert versions.status_code == 200, f"the login key could not read the registry: {versions.status_code}"

    replay = _exchange(code, verifier)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_a_wrong_verifier_is_refused(approve: Callable[[str], str]) -> None:
    """A code is bound to its challenge, so another verifier gets nothing."""
    code = approve(secrets.token_urlsafe(48))
    response = _exchange(code, secrets.token_urlsafe(48))
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert "access_token" not in response.json()
