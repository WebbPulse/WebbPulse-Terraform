"""The VCS binding fields on a workspace.

A workspace binds to a repository by `owner/name`, stored lowercased in the key the
binding index reads, and by the GitHub repository id the first upload records. The
id is what survives a rename, so a change of repository has to drop it.
"""

import pytest

from app.common.db import repositories
from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"


def _stored(settings, workspace_id: str) -> dict:
    """The raw row, attributes the API does not render included."""
    return repositories.workspaces(settings).get({"workspace_id": workspace_id}) or {}


@pytest.fixture
def bound(auth_client):
    """A workspace bound to a repository and tracking `main`."""
    payload = WORKSPACE_PAYLOAD | {
        "name": "bound",
        "vcs_repo": "WebbPulse/Example",
        "tracked_branch": "main",
        "trigger_patterns": ["stacks/**"],
    }
    response = auth_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_create_stores_the_binding(bound, settings):
    """The name keeps its case and the index key is lowercased."""
    assert bound["vcs_repo"] == "WebbPulse/Example"
    assert bound["tracked_branch"] == "main"
    assert bound["trigger_patterns"] == ["stacks/**"]
    assert bound["speculative_plans"] is True
    assert bound["vcs_repository_id"] is None
    assert _stored(settings, bound["workspace_id"])["vcs_repo_key"] == "webbpulse/example"


def test_an_unbound_workspace_has_no_index_key(auth_client, workspace, settings):
    """A sparse index: an unbound workspace never appears in it."""
    assert workspace["vcs_repo"] is None
    assert workspace["trigger_patterns"] == []
    assert "vcs_repo_key" not in _stored(settings, workspace["workspace_id"])


@pytest.mark.parametrize("repo", ["no-slash", "a/b/c", "owner/", "own er/name"])
def test_a_malformed_repository_is_a_422(auth_client, repo):
    """Only `owner/name`."""
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"vcs_repo": repo})
    assert response.status_code == 422


def test_changing_the_repository_drops_the_recorded_id(auth_client, bound, settings):
    """The id belonged to the previous repository."""
    repositories.workspaces(settings).update(
        {"workspace_id": bound["workspace_id"]},
        update_expression="SET vcs_repository_id = :id",
        expression_values={":id": "424242"},
    )
    response = auth_client.patch(f"{BASE}/{bound['workspace_id']}", json={"vcs_repo": "WebbPulse/Other"})
    assert response.status_code == 200, response.text
    assert response.json()["vcs_repository_id"] is None
    row = _stored(settings, bound["workspace_id"])
    assert row["vcs_repo_key"] == "webbpulse/other"
    assert "vcs_repository_id" not in row


def test_an_unrelated_edit_keeps_the_recorded_id(auth_client, bound, settings):
    """Only a repository change drops it."""
    repositories.workspaces(settings).update(
        {"workspace_id": bound["workspace_id"]},
        update_expression="SET vcs_repository_id = :id",
        expression_values={":id": "424242"},
    )
    response = auth_client.patch(f"{BASE}/{bound['workspace_id']}", json={"tracked_branch": "release"})
    assert response.json()["tracked_branch"] == "release"
    assert response.json()["vcs_repository_id"] == "424242"


def test_clearing_the_repository_removes_the_binding(auth_client, bound, settings):
    """A null clears the name, the index key and the id."""
    response = auth_client.patch(f"{BASE}/{bound['workspace_id']}", json={"vcs_repo": None})
    assert response.status_code == 200, response.text
    assert response.json()["vcs_repo"] is None
    row = _stored(settings, bound["workspace_id"])
    assert "vcs_repo" not in row
    assert "vcs_repo_key" not in row


def test_the_other_vcs_fields_clear_with_a_null(auth_client, bound, settings):
    """The branch and the patterns read back as their defaults."""
    response = auth_client.patch(
        f"{BASE}/{bound['workspace_id']}", json={"tracked_branch": None, "trigger_patterns": None}
    )
    assert response.json()["tracked_branch"] is None
    assert response.json()["trigger_patterns"] == []


def test_speculative_plans_can_be_turned_off(auth_client, bound):
    """A workspace can opt out of pull request plans."""
    response = auth_client.patch(f"{BASE}/{bound['workspace_id']}", json={"speculative_plans": False})
    assert response.json()["speculative_plans"] is False
