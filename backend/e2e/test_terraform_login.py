"""`terraform login` against the deployed staging stage, driven headlessly.

Terraform CLI reads `login.v1` from the SPA host's discovery document, opens the approve
page with its PKCE challenge and a loopback redirect, and exchanges the code the page hands
back at the token endpoint. These cases play both halves: the run's signed-in user approves
through `POST /api/v1/oauth/authorizations` (step-up gated, as the approve page is), and the
exchange posts the form Terraform's oauth2 client sends, with plain `httpx` and no gate
header, exactly as the CLI does. The last case hands the key to `wp-tf` and plans with it.
The minted `terraform login` keys are revoked on teardown, whatever the outcome. Skipped
outside staging and on the read-only production smoke, since a key is minted. No key, code or verifier is ever printed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
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


WP_TF_PLAN_TIMEOUT_SECONDS = 900
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "first-run"


def _wp_tf() -> list[str]:
    """The installed `wp-tf` console script, from the `tf` extra of webbpulse."""
    script = Path(sys.executable).with_name("wp-tf")
    if not script.exists():
        pytest.fail(f"{script} is missing; the e2e group must install webbpulse[tf]")
    return [str(script)]


def test_wp_tf_plans_with_the_login_key(
    approve: Callable[[str], str],
    workspace: dict[str, Any],
    gate_headers: Mapping[str, str],
    api: Any,
    tmp_path: Path,
) -> None:
    """`wp-tf plan` reads the key `terraform login` stored and streams a plan-only run, which has nothing to confirm.

    The key is written to a throwaway `credentials.tfrc.json` under a temporary HOME, the
    way `terraform login` leaves it, so the CLI is exercised exactly as a workstation runs
    it. `wp-tf` reads the gate value from its SSM parameter itself, and never prints it.
    """
    verifier = secrets.token_urlsafe(48)
    exchanged = _exchange(approve(verifier), verifier)
    assert exchanged.status_code == 200, f"the exchange answered {exchanged.status_code}"
    token = exchanged.json()["access_token"]
    terraform_d = tmp_path / ".terraform.d"
    terraform_d.mkdir()
    (terraform_d / "credentials.tfrc.json").write_text(json.dumps({"credentials": {REGISTRY_HOST: {"token": token}}}))

    env = {key: value for key, value in os.environ.items() if not key.startswith(("WP_TF_", "TF_TOKEN_"))}
    env["HOME"] = str(tmp_path)
    workspace_id = str(workspace["workspace_id"])
    completed = subprocess.run(
        [*_wp_tf(), "--host", REGISTRY_HOST, "plan", str(EXAMPLE), "-w", workspace_id, "-m", "e2e wp-tf"],
        env=env,
        capture_output=True,
        text=True,
        timeout=WP_TF_PLAN_TIMEOUT_SECONDS,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert token not in output, "wp-tf printed the key"
    gate = next(iter(gate_headers.values()), "")
    assert not gate or gate not in output, "wp-tf printed the gate value"
    assert completed.returncode == 0, f"wp-tf plan exited {completed.returncode}: {completed.stderr[-1500:]}"
    assert "terraform plan" in completed.stdout, "wp-tf streamed no plan log"
    found = re.search(r"run (run-[0-9A-Z]+) ", completed.stderr)
    assert found, f"wp-tf named no run: {completed.stderr[-400:]}"
    run_id = found.group(1)

    run = api.get(f"/api/v1/runs/{run_id}")
    assert run.status_code == 200, run.text[:400]
    assert run.json()["status"] == "planned_and_finished", run.text[:400]
    assert run.json()["workspace_id"] == workspace_id

    status = subprocess.run(
        [*_wp_tf(), "--host", REGISTRY_HOST, "status", run_id],
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS * 2,
        check=False,
    )
    assert status.returncode == 0, status.stderr[-400:]
    assert json.loads(status.stdout)["run_id"] == run_id

    confirm = httpx.post(
        f"https://{API_HOST}/api/v1/runs/{run_id}/confirm",
        headers={"Authorization": f"Bearer {token}", **gate_headers},
        timeout=TIMEOUT_SECONDS,
    )
    assert confirm.status_code == 409, f"a login key confirming a plan-only run answered {confirm.status_code}"
