"""VCS run reporting against the deployed stage, read back from the connected repository.

VCS runs start from GitHub App webhook deliveries, which only GitHub can sign, so the
case starts none. It reads what the stage's App has already reported on the repository
`E2E_VCS_CONNECT_REPO` names: the newest commits on its default branch are searched for
a `webbpulse-terraform/<workspace>` check run whose details link points at this stage.
That check's `external_id` is a run id, and the run has to be readable through the API
as a VCS run of the same repository and commit, with the aggregate
`webbpulse-terraform` check from the same App on the same commit.

Check runs on a public repository are readable without credentials, and `GITHUB_TOKEN`
is used when the job exposes one. The case skips when the job names no repository or
when no recent commit carries a check from this stage. Nothing is created.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

REPOSITORY_VARIABLE = "E2E_VCS_CONNECT_REPO"
GITHUB_API = "https://api.github.com"
AGGREGATE_CHECK = "webbpulse-terraform"
WORKSPACE_CHECK_PREFIX = f"{AGGREGATE_CHECK}/"
COMMIT_WINDOW = 10


def _repository() -> str:
    """The repository to read, or a skip when the job names none."""
    repository = os.environ.get(REPOSITORY_VARIABLE, "").strip()
    if not repository:
        pytest.skip(f"{REPOSITORY_VARIABLE} is unset, so there is no connected repository to read")
    return repository


def _github(path: str, **params: Any) -> Any:
    """One GitHub REST read. Never prints the token."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(f"{GITHUB_API}{path}", params=params, headers=headers, timeout=30)
    if response.status_code in (403, 429) and response.headers.get("x-ratelimit-remaining") == "0":
        pytest.skip("the GitHub API rate limit is spent, so the repository cannot be read")
    assert response.status_code == 200, f"GET {path} answered {response.status_code}"
    return response.json()


def _stage_checks(repository: str, sha: str, stage_url: str) -> list[dict[str, Any]]:
    """The check runs on `sha` whose details link points at this stage."""
    body = _github(f"/repos/{repository}/commits/{sha}/check-runs", filter="all", per_page=100)
    return [
        run
        for run in body.get("check_runs", [])
        if str(run.get("name", "")).startswith(AGGREGATE_CHECK)
        and str(run.get("details_url") or "").startswith(stage_url)
    ]


@pytest.mark.e2e_writes
def test_a_vcs_run_is_reported_as_a_check_run(api: Any, e2e_env: Any) -> None:
    """A workspace check links to a VCS run of the same commit, beside the aggregate."""
    repository = _repository()
    stage_url = e2e_env.web_base_url.rstrip("/")
    default_branch = str(_github(f"/repos/{repository}").get("default_branch") or "main")
    commits = _github(f"/repos/{repository}/commits", sha=default_branch, per_page=COMMIT_WINDOW)

    for commit in commits:
        sha = str(commit["sha"])
        checks = _stage_checks(repository, sha, stage_url)
        workspace_checks = [
            run for run in checks if str(run["name"]).startswith(WORKSPACE_CHECK_PREFIX) and run.get("external_id")
        ]
        if not workspace_checks:
            continue
        check = workspace_checks[0]
        run_id = str(check["external_id"])
        assert str(check["details_url"]).endswith(f"/runs/{run_id}"), check["details_url"]
        assert any(run["name"] == AGGREGATE_CHECK for run in checks), f"no {AGGREGATE_CHECK} check on {sha}"
        app_ids = {(run.get("app") or {}).get("id") for run in checks}
        assert len(app_ids) == 1, f"the stage's checks on {sha} came from more than one App"

        response = api.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200, response.text[:400]
        run = response.json()
        assert str(run["source"]).startswith("vcs_"), run["source"]
        vcs = run["vcs"] or {}
        assert str(vcs.get("repo", "")).lower() == repository.lower()
        assert sha in {vcs.get("sha"), vcs.get("head_sha")}
        return

    pytest.skip(f"none of the newest {COMMIT_WINDOW} commits on {repository} carries a check from this stage")
