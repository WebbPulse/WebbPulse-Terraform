"""`wp-tf login` against the deployed staging stage: the OAuth device grant, driven headlessly.

The CLI half posts to `/api/auth/device/code` and polls `/api/auth/device/token` with plain
`httpx` and the gate header, exactly as `wp-tf login` does. The browser half is the run's
signed-in user, stepped up so the sign-in is fresh, opening the approval page with its bearer
and posting the signed form back with the issuer's `Origin`, as the page's own form would.
The device access token then reads the API, drives a `wp-tf apply` and its destroy, and stops
working once its grant is revoked. Every grant is revoked on teardown, whatever the outcome. Skipped outside
staging, since a grant is written. No code, token or signature is ever printed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

REGISTRY_HOST = "staging.terraform.webbpulse.com"
API_HOST = "api.staging.terraform.webbpulse.com"
ISSUER = f"https://{API_HOST}/api/auth"
ISSUER_ORIGIN = f"https://{API_HOST}"
CLIENT_ID = "wp-tf"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT_SECONDS = 30
POLL_LIMIT_SECONDS = 60
LIVENESS_WAIT_SECONDS = 7
WP_TF_APPLY_TIMEOUT_SECONDS = 1500
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "first-run"

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.xdist_group("device-login"),
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="a device login writes a grant, and the gate header is a staging concern",
    ),
]


@dataclass(frozen=True)
class DeviceSession:
    """The tokens one approved device login returned."""

    access_token: str
    refresh_token: str
    scope: str


def _hidden(html: str, name: str) -> str:
    """The value of one hidden input on the approval form."""
    found = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)"', html)
    assert found, f"the approval page carried no {name} field"
    return unescape(found.group(1))


def _start(gate_headers: Mapping[str, str]) -> dict[str, Any]:
    """Start a device login as `wp-tf login` does, with the standard scopes."""
    response = httpx.post(
        f"{ISSUER}/device/code",
        data={"client_id": CLIENT_ID},
        headers=dict(gate_headers),
        timeout=TIMEOUT_SECONDS,
    )
    assert response.status_code == 200, f"starting answered {response.status_code}: {response.text[:300]}"
    assert response.headers.get("cache-control") == "no-store"
    body: dict[str, Any] = response.json()
    assert body["verification_uri"] == f"{ISSUER}/device"
    return body


def _approve(api: Any, user_code: str, *, decision: str = "allow") -> httpx.Response:
    """Open the approval page as the signed-in user and post its form back."""
    shown = api.get("/api/auth/device", params={"user_code": user_code}, headers={"accept": "text/html"})
    assert shown.status_code == 200, f"the approval page answered {shown.status_code}"
    form = {
        "user_code": _hidden(shown.text, "user_code"),
        "signature": _hidden(shown.text, "signature"),
        "decision": decision,
    }
    return api.post(
        "/api/auth/device/approve",
        data=form,
        headers={"origin": ISSUER_ORIGIN, "accept": "text/html"},
    )


def _poll(device_code: str, interval: int, gate_headers: Mapping[str, str]) -> httpx.Response:
    """Poll the token endpoint until it answers something other than pending."""
    deadline = time.monotonic() + POLL_LIMIT_SECONDS
    while True:
        response = httpx.post(
            f"{ISSUER}/device/token",
            data={"grant_type": DEVICE_GRANT_TYPE, "device_code": device_code, "client_id": CLIENT_ID},
            headers=dict(gate_headers),
            timeout=TIMEOUT_SECONDS,
        )
        error = response.json().get("error") if response.status_code == 400 else None
        if error not in {"authorization_pending", "slow_down"} or time.monotonic() > deadline:
            return response
        interval += 5 if error == "slow_down" else 0
        time.sleep(interval)


def _revoke(refresh_token: str, gate_headers: Mapping[str, str]) -> int:
    """Revoke the grant behind a refresh token, as `wp-tf logout` does."""
    response = httpx.post(
        f"{ISSUER}/device/revoke",
        data={"token": refresh_token, "client_id": CLIENT_ID},
        headers=dict(gate_headers),
        timeout=TIMEOUT_SECONDS,
    )
    return response.status_code


@pytest.fixture
def device_login(
    step_up_again: Callable[[], Any], gate_headers: Mapping[str, str]
) -> Iterator[Callable[[], DeviceSession]]:
    """Run whole device logins as the run's user, revoking every grant they open on teardown."""
    opened: list[str] = []

    def _login() -> DeviceSession:
        """Start, approve and poll one device login."""
        started = _start(gate_headers)
        approved = _approve(step_up_again(), started["user_code"])
        assert approved.status_code == 200, f"approving answered {approved.status_code}"
        assert "Device connected" in approved.text
        response = _poll(started["device_code"], int(started.get("interval", 5)), gate_headers)
        assert response.status_code == 200, f"polling answered {response.status_code}: {response.text[:300]}"
        body = response.json()
        opened.append(body["refresh_token"])
        assert body["token_type"] == "Bearer"
        assert not body["access_token"].startswith("wpk_"), "the device login issued a long-lived key"
        return DeviceSession(body["access_token"], body["refresh_token"], str(body.get("scope", "")))

    yield _login

    failures = [str(status) for status in (_revoke(token, gate_headers) for token in opened) if status != 200]
    if failures:
        pytest.fail(f"e2e teardown could not revoke device grants: {', '.join(failures)}")


def test_a_device_token_reads_the_api_until_revoked(
    device_login: Callable[[], DeviceSession], api: Any, gate_headers: Mapping[str, str]
) -> None:
    """An approved device login reads the API, is listed for its user, and stops once revoked."""
    session = device_login()
    granted = session.scope.split()
    assert "runs:apply" in granted, "a default device login cannot apply"
    assert not {"state:download", "state:write", "admin"} & set(granted), "an explicit scope was granted by default"
    device = api.with_token(session.access_token)
    workspaces = device.get("/api/v1/workspaces")
    assert workspaces.status_code == 200, f"a device token reading workspaces answered {workspaces.status_code}"

    listed = api.get("/api/auth/device/grants")
    assert listed.status_code == 200, f"listing device logins answered {listed.status_code}"
    assert any(grant.get("client_id") == CLIENT_ID for grant in listed.json()["grants"])

    assert _revoke(session.refresh_token, gate_headers) == 200
    time.sleep(LIVENESS_WAIT_SECONDS)
    after = device.get("/api/v1/workspaces")
    assert after.status_code == 401, f"a revoked device token answered {after.status_code}"


PROVIDER_PROTOCOL_URL = f"https://{API_HOST}/v1/providers/WebbPulse/webbpulse"


def test_a_device_token_downloads_a_provider_until_revoked(
    device_login: Callable[[], DeviceSession], gate_headers: Mapping[str, str]
) -> None:
    """A `wp-tf login` token reads the provider protocol and its checksums, and stops once revoked.

    The protocol routes have no gateway authorizer, so the registry function verifies the
    token itself; this proves that path, liveness included, on the deployed stage.
    """
    session = device_login()
    assert "registry:read" in session.scope.split(), "a default device login cannot read the registry"
    headers = {"Authorization": f"Bearer {session.access_token}"}
    versions = httpx.get(f"{PROVIDER_PROTOCOL_URL}/versions", headers=headers, timeout=TIMEOUT_SECONDS)
    if versions.status_code == 404:
        pytest.skip("the WebbPulse/webbpulse provider is not connected on staging")
    assert versions.status_code == 200, f"a device token listing versions answered {versions.status_code}"
    listed = [row for row in versions.json()["versions"] if {"os": "linux", "arch": "amd64"} in row["platforms"]]
    assert listed, "no published version carries linux_amd64"
    download_url = f"{PROVIDER_PROTOCOL_URL}/{listed[0]['version']}/download/linux/amd64"
    download = httpx.get(download_url, headers=headers, timeout=TIMEOUT_SECONDS)
    assert download.status_code == 200, f"a device token downloading answered {download.status_code}"
    body = download.json()
    sums = httpx.get(body["shasums_url"], timeout=TIMEOUT_SECONDS)
    assert sums.status_code == 200
    assert f"{body['shasum']}  {body['filename']}" in sums.text

    assert _revoke(session.refresh_token, gate_headers) == 200
    time.sleep(LIVENESS_WAIT_SECONDS)
    after = httpx.get(download_url, headers=headers, timeout=TIMEOUT_SECONDS)
    assert after.status_code == 401, f"a revoked device token downloading answered {after.status_code}"


def test_a_denied_device_login_issues_nothing(
    step_up_again: Callable[[], Any], gate_headers: Mapping[str, str]
) -> None:
    """Denying the approval leaves the CLI with `access_denied` and no token."""
    started = _start(gate_headers)
    denied = _approve(step_up_again(), started["user_code"], decision="deny")
    assert denied.status_code == 200, f"denying answered {denied.status_code}"
    response = _poll(started["device_code"], int(started.get("interval", 5)), gate_headers)
    assert response.status_code == 400
    assert response.json()["error"] == "access_denied"
    assert "access_token" not in response.json()


def test_approval_from_another_origin_is_refused(
    step_up_again: Callable[[], Any], gate_headers: Mapping[str, str]
) -> None:
    """The approval POST is refused unless it comes from the issuer's own page."""
    started = _start(gate_headers)
    api = step_up_again()
    shown = api.get("/api/auth/device", params={"user_code": started["user_code"]}, headers={"accept": "text/html"})
    assert shown.status_code == 200
    forged = api.post(
        "/api/auth/device/approve",
        data={
            "user_code": _hidden(shown.text, "user_code"),
            "signature": _hidden(shown.text, "signature"),
            "decision": "allow",
        },
        headers={"origin": "https://attacker.example", "accept": "text/html"},
    )
    assert forged.status_code == 403, f"a cross-origin approval answered {forged.status_code}"


def test_signed_out_approval_hands_off_to_the_sign_in_page(api: Any, gate_headers: Mapping[str, str]) -> None:
    """A browser with no session is sent to the SPA's sign-in page with the approval to come back to."""
    started = _start(gate_headers)
    anonymous = api.with_token(None)
    response = anonymous.get("/api/auth/device", params={"user_code": started["user_code"]})
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"https://{REGISTRY_HOST}/sign-in"
    assert "returnTo=" in location.query


def _wp_tf() -> list[str]:
    """The installed `wp-tf` console script, from the `tf` extra of webbpulse."""
    script = Path(sys.executable).with_name("wp-tf")
    if not script.exists():
        pytest.fail(f"{script} is missing; the e2e group must install webbpulse[tf]")
    return [str(script)]


def _run_wp_tf(env: Mapping[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one `wp-tf` command against the staging host, never raising on its exit code."""
    return subprocess.run(
        [*_wp_tf(), "--host", REGISTRY_HOST, *arguments],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=WP_TF_APPLY_TIMEOUT_SECONDS,
        check=False,
    )


def test_wp_tf_applies_with_a_device_token(
    device_login: Callable[[], DeviceSession],
    workspace: dict[str, Any],
    gate_headers: Mapping[str, str],
    api: Any,
    tmp_path: Path,
) -> None:
    """`wp-tf apply` on a default device login plans, confirms as the person and applies.

    The destroy that follows leaves the workspace managing nothing, so teardown's safe
    delete passes.
    """
    session = device_login()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("WP_TF_", "TF_TOKEN_"))}
    env["HOME"] = str(tmp_path)
    env["WP_TF_TOKEN"] = session.access_token
    workspace_id = str(workspace["workspace_id"])
    gate = next(iter(gate_headers.values()), "")

    for extra in ((), ("--destroy",)):
        completed = _run_wp_tf(
            env, "apply", str(EXAMPLE), "-w", workspace_id, "-m", "e2e device apply", "--auto-approve", *extra
        )
        output = completed.stdout + completed.stderr
        assert session.access_token not in output, "wp-tf printed the token"
        assert not gate or gate not in output, "wp-tf printed the gate value"
        assert completed.returncode == 0, (
            f"wp-tf apply {extra} exited {completed.returncode}: {completed.stderr[-1500:]}"
        )
        found = re.search(r"run (run-[0-9A-Z]+) ", completed.stderr)
        assert found, f"wp-tf named no run: {completed.stderr[-400:]}"

        run = api.get(f"/api/v1/runs/{found.group(1)}")
        assert run.status_code == 200, run.text[:400]
        body = run.json()
        assert body["status"] == "applied", run.text[:400]
        decision = body.get("decision") or {}
        assert decision.get("action") == "confirmed", decision
        assert (decision.get("actor") or {}).get("id") not in (None, "", "auto-apply"), decision
