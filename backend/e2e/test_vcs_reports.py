"""VCS run reporting against the deployed stage: a VCS run shows up as a GitHub check.

The upload goes through the same path as `test_vcs_ingest`, and the case then waits
for the run's `webbpulse-terraform/<workspace>` check run on the commit, matched by
the run id GitHub holds as its `external_id`. Check runs on a public repository are
readable without credentials, and `GITHUB_TOKEN` is used when the job exposes one.

The case runs only where `E2E_VCS_REPORTS` is set and the job can mint an OIDC token,
and skips when the stage's App is not installed on the repository, so a stage with
no App skips cleanly. The configuration declares nothing and the workspace fixture's
teardown ends the run, so nothing billable is left behind.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any

import httpx
import pytest
from test_vcs_ingest import (
    REQUEST_TOKEN_VARIABLE,
    REQUEST_URL_VARIABLE,
    SUPPORTED_EVENTS,
    config_tarball,
    mint_token,
    unverified_claims,
)

ENABLE_VARIABLE = "E2E_VCS_REPORTS"
GITHUB_API = "https://api.github.com"
POLL_SECONDS = 10
REPORT_TIMEOUT_SECONDS = 240

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get(ENABLE_VARIABLE)
        and os.environ.get(REQUEST_URL_VARIABLE)
        and os.environ.get(REQUEST_TOKEN_VARIABLE)
    ),
    reason=f"{ENABLE_VARIABLE} is unset or the job cannot mint a GitHub OIDC token",
)


def _head_sha(event: str, claims: dict[str, Any]) -> str | None:
    """The commit the report lands on: the pushed commit, or the pull request's head."""
    if event == "push":
        return str(claims["sha"])
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    head = (payload.get("pull_request") or {}).get("head") or {}
    return str(head["sha"]) if head.get("sha") else None


def _check_runs(repository: str, sha: str, name: str) -> list[dict[str, Any]]:
    """The check runs named `name` on `sha`. Never prints the token."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(
        f"{GITHUB_API}/repos/{repository}/commits/{sha}/check-runs",
        params={"check_name": name, "filter": "all"},
        headers=headers,
        timeout=30,
    )
    assert response.status_code == 200, f"listing check runs answered {response.status_code}"
    return list(response.json().get("check_runs", []))


@pytest.mark.e2e_writes
def test_a_vcs_run_is_reported_as_a_check_run(api: Any, e2e_env: Any, workspace: dict[str, Any]) -> None:
    """The run's own check run appears on its commit, linking back to the run."""
    token = mint_token()
    claims = unverified_claims(token)
    event = str(claims.get("event_name", ""))
    if event not in SUPPORTED_EVENTS:
        pytest.skip(f"the job's {event} event starts no VCS run")
    head = _head_sha(event, claims)
    if head is None:
        pytest.skip("the pull request event payload names no head commit")

    directory = f"e2e/{secrets.token_hex(4)}"
    patch: dict[str, Any] = {"vcs_repo": claims["repository"], "working_directory": directory}
    if event == "push":
        patch["tracked_branch"] = str(claims.get("ref", "")).removeprefix("refs/heads/")
    updated = api.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json=patch)
    if updated.status_code == 422 and "VCS_REPO_NOT_INSTALLED" in updated.text:
        pytest.skip("the stage's GitHub App is not installed on this repository")
    assert updated.status_code == 200, updated.text[:400]
    if not updated.json().get("vcs_installation_id"):
        pytest.skip("the stage has no GitHub App to report through")

    data = config_tarball(directory)
    requested = httpx.post(
        f"{e2e_env.api_base_url.rstrip('/')}/api/v1/vcs/uploads",
        json={"sha": head, "pr_number": None, "base_sha": None, "size_bytes": len(data)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert requested.status_code == 201, f"the upload request answered {requested.status_code}"
    body = requested.json()
    put = httpx.put(body["upload_url"], content=data, headers=body["headers"], timeout=60)
    assert put.status_code in (200, 204), f"the presigned PUT answered {put.status_code}"

    name = f"webbpulse-terraform/{workspace['name']}"
    deadline = time.monotonic() + REPORT_TIMEOUT_SECONDS
    run_id = ""
    found: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        if not run_id:
            listed = api.get("/api/v1/runs", params={"workspace_id": workspace["workspace_id"]})
            assert listed.status_code == 200, listed.text[:400]
            runs = [item for item in listed.json()["items"] if str(item.get("source", "")).startswith("vcs_")]
            run_id = str(runs[0]["run_id"]) if runs else ""
        if run_id:
            found = [run for run in _check_runs(str(claims["repository"]), head, name) if run["external_id"] == run_id]
            if found:
                break
        time.sleep(POLL_SECONDS)
    assert run_id, "no VCS run reached the workspace"
    assert found, f"no {name} check run for {run_id} appeared within {REPORT_TIMEOUT_SECONDS}s"
    assert found[0]["details_url"].endswith(f"/runs/{run_id}")
