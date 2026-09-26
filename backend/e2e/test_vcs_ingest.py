"""The VCS bridge's ingest path against the deployed stage.

A GitHub Actions job's own OIDC token asks `POST /api/v1/vcs/uploads` for a URL, the
tarball is PUT to it, and the S3 event, the queue and the runs function turn it into a
run on the bound workspace. Only a job holding `id-token: write` can mint that token,
so the case runs only where `E2E_VCS_INGEST` is set and the job exposes the token
request variables, and only for a push or pull request event, since the route refuses
the rest.

The configuration declares nothing, and whatever run the upload started is ended by the
workspace fixture's teardown, so the case leaves no execution behind.
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import tarfile
import time
from typing import Any

import httpx
import pytest

ENABLE_VARIABLE = "E2E_VCS_INGEST"
AUDIENCE_VARIABLE = "E2E_VCS_OIDC_AUDIENCE"
DEFAULT_AUDIENCE = "webbpulse-terraform"
REQUEST_URL_VARIABLE = "ACTIONS_ID_TOKEN_REQUEST_URL"
REQUEST_TOKEN_VARIABLE = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"
SUPPORTED_EVENTS = ("push", "pull_request")
POLL_SECONDS = 5
INGEST_TIMEOUT_SECONDS = 180

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get(ENABLE_VARIABLE)
        and os.environ.get(REQUEST_URL_VARIABLE)
        and os.environ.get(REQUEST_TOKEN_VARIABLE)
    ),
    reason=f"{ENABLE_VARIABLE} is unset or the job cannot mint a GitHub OIDC token",
)


def _mint_token() -> str:
    """A GitHub Actions OIDC token for the configured audience. Never printed."""
    audience = os.environ.get(AUDIENCE_VARIABLE, "").strip() or DEFAULT_AUDIENCE
    response = httpx.get(
        os.environ[REQUEST_URL_VARIABLE],
        params={"audience": audience},
        headers={"Authorization": f"bearer {os.environ[REQUEST_TOKEN_VARIABLE]}"},
        timeout=30,
    )
    assert response.status_code == 200, f"minting the OIDC token answered {response.status_code}"
    return str(response.json()["value"])


def _unverified_claims(token: str) -> dict[str, Any]:
    """The token's payload, read only to shape the workspace; the server verifies it."""
    payload = token.split(".")[1]
    return dict(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))


def _tarball(directory: str) -> bytes:
    """A configuration declaring nothing under `directory`, with its changed paths list."""
    contents = {
        f"{directory}/main.tf": "terraform {}\n",
        ".webbpulse/changed-paths.txt": f"{directory}/main.tf\n",
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in contents.items():
            data = text.encode()
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.e2e_writes
def test_an_upload_starts_a_vcs_run(api: Any, e2e_env: Any, workspace: dict[str, Any]) -> None:
    """A bound workspace gets a run sourced from the upload, carrying the token's commit."""
    token = _mint_token()
    claims = _unverified_claims(token)
    event = str(claims.get("event_name", ""))
    if event not in SUPPORTED_EVENTS:
        pytest.skip(f"the job's {event} event starts no VCS run")

    directory = f"e2e/{secrets.token_hex(4)}"
    patch: dict[str, Any] = {"vcs_repo": claims["repository"], "working_directory": directory}
    ref = str(claims.get("ref", ""))
    if event == "push":
        patch["tracked_branch"] = ref.removeprefix("refs/heads/")
    updated = api.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json=patch)
    assert updated.status_code == 200, updated.text[:400]

    data = _tarball(directory)
    requested = httpx.post(
        f"{e2e_env.api_base_url.rstrip('/')}/api/v1/vcs/uploads",
        json={"sha": claims["sha"], "pr_number": None, "base_sha": None, "size_bytes": len(data)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert requested.status_code == 201, f"the upload request answered {requested.status_code}: {requested.text[:400]}"
    body = requested.json()
    put = httpx.put(body["upload_url"], content=data, headers=body["headers"], timeout=60)
    assert put.status_code in (200, 204), f"the presigned PUT answered {put.status_code}"

    deadline = time.monotonic() + INGEST_TIMEOUT_SECONDS
    runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        listed = api.get("/api/v1/runs", params={"workspace_id": workspace["workspace_id"]})
        assert listed.status_code == 200, listed.text[:400]
        runs = [item for item in listed.json()["items"] if str(item.get("source", "")).startswith("vcs_")]
        if runs:
            break
        time.sleep(POLL_SECONDS)
    assert runs, f"no VCS run reached the workspace within {INGEST_TIMEOUT_SECONDS}s"

    [run] = runs
    assert run["source"] == ("vcs_push" if event == "push" else "vcs_pr")
    assert run["plan_only"] is (event == "pull_request")
    assert run["vcs"]["repository_id"] == str(claims["repository_id"])
    assert run["vcs"]["sha"] == claims["sha"]
    assert run["actor"]["kind"] == "vcs"
