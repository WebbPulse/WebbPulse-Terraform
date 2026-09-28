"""Connecting a workspace to a repository against the deployed stage.

The stage resolves the repository through its GitHub App, so the connected workspace
carries the repository id, the installation and the default branch without anyone
typing them. The repository is `E2E_VCS_CONNECT_REPO`, else the repository the job runs
in. A stage with no App binds by name only and one whose App is not installed on the
repository refuses it, and both skip the resolution case rather than fail it.

The workspace watches a random directory no commit touches and takes no pull request
plans, so a real upload arriving while it is connected starts nothing, and it is
disconnected before the fixture deletes it. Nothing billable is created.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

import pytest

REPOSITORY_VARIABLE = "E2E_VCS_CONNECT_REPO"


def _repository() -> str:
    """The repository to connect, or a skip when the job names none."""
    repository = os.environ.get(REPOSITORY_VARIABLE, "").strip() or os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repository:
        pytest.skip(f"{REPOSITORY_VARIABLE} is unset and the job names no repository")
    return repository


@pytest.mark.e2e_writes
def test_connecting_resolves_the_repository_through_the_app(api: Any, workspace: dict[str, Any]) -> None:
    """The id, installation and default branch come from the App, and a null disconnects."""
    repository = _repository()
    url = f"/api/v1/workspaces/{workspace['workspace_id']}"
    directory = f"e2e/{secrets.token_hex(4)}"
    connected = api.patch(
        url,
        json={"vcs_repo": repository, "working_directory": directory, "speculative_plans": False},
    )
    try:
        if connected.status_code == 422 and "VCS_REPO_NOT_INSTALLED" in connected.text:
            pytest.skip("the stage's GitHub App is not installed on this repository")
        assert connected.status_code == 200, connected.text[:400]
        body = connected.json()
        if body.get("vcs_repository_id") is None:
            pytest.skip("the stage has no GitHub App, so the binding is by name only")
        assert body["vcs_repo"].lower() == repository.lower()
        assert body["vcs_installation_id"]
        assert body["tracked_branch"]
        assert body["working_directory"] == directory
    finally:
        disconnected = api.patch(url, json={"vcs_repo": None, "tracked_branch": None})
        assert disconnected.status_code == 200, disconnected.text[:400]
    after = disconnected.json()
    assert after["vcs_repo"] is None
    assert after["vcs_repository_id"] is None
    assert after["vcs_installation_id"] is None


@pytest.mark.e2e_writes
@pytest.mark.parametrize("directory", ["../outside", "/absolute"])
def test_a_working_directory_outside_the_repository_is_refused(
    api: Any, workspace: dict[str, Any], directory: str
) -> None:
    """The deployed validation refuses what the runner could never resolve."""
    response = api.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"working_directory": directory})
    assert response.status_code == 422, response.text[:400]
