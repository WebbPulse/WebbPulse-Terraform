"""Projects: the default project, the CRUD routes and moving workspaces between them."""

from __future__ import annotations

from typing import Any

from app.common.db import repositories
from tests.conftest import WORKSPACE_PAYLOAD

PROJECTS = "/api/v1/projects"
WORKSPACES = "/api/v1/workspaces"
DEFAULT = "prj-default"


def create_project(auth_client, name: str, **extra: Any) -> dict[str, Any]:
    """Create a project and return it."""
    response = auth_client.post(PROJECTS, json={"name": name, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def test_the_default_project_is_listed_first_and_holds_existing_workspaces(auth_client, workspace):
    """A workspace stored without a project is in the default, with nothing written to it."""
    stored = repositories.workspaces().get({"workspace_id": workspace["workspace_id"]})
    assert stored is not None
    assert "project_id" not in stored
    assert workspace["project_id"] == DEFAULT

    create_project(auth_client, "Alpha")
    items = auth_client.get(PROJECTS).json()["items"]

    assert [item["name"] for item in items] == ["Default Project", "Alpha"]
    assert items[0]["is_default"] is True
    assert items[0]["workspace_count"] == 1
    assert items[1]["workspace_count"] == 0


def test_a_project_is_created_read_renamed_and_deleted(auth_client):
    """The full lifecycle of an empty project."""
    created = create_project(auth_client, "Platform", description="Shared wiring.")
    assert created["project_id"].startswith("prj-")
    assert created["description"] == "Shared wiring."

    project_id = created["project_id"]
    assert auth_client.get(f"{PROJECTS}/{project_id}").json()["name"] == "Platform"

    renamed = auth_client.patch(f"{PROJECTS}/{project_id}", json={"name": "Platform team", "description": None})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Platform team"
    assert renamed.json()["description"] == ""

    assert auth_client.delete(f"{PROJECTS}/{project_id}").status_code == 204
    assert auth_client.get(f"{PROJECTS}/{project_id}").status_code == 404


def test_a_project_name_is_unique_ignoring_case(auth_client):
    """A second project with the same name in another case, or the default's name, is refused."""
    create_project(auth_client, "Standupless")
    duplicate = auth_client.post(PROJECTS, json={"name": "standupless"})
    assert duplicate.status_code == 409
    assert "PROJECT_NAME_TAKEN" in duplicate.text
    assert auth_client.post(PROJECTS, json={"name": "default project"}).status_code == 409


def test_the_default_project_cannot_be_changed_or_deleted(auth_client):
    """The default project is not stored, so an edit or a delete is refused."""
    assert auth_client.get(f"{PROJECTS}/{DEFAULT}").json()["is_default"] is True
    patched = auth_client.patch(f"{PROJECTS}/{DEFAULT}", json={"name": "Renamed"})
    assert patched.status_code == 409
    assert "DEFAULT_PROJECT_READ_ONLY" in patched.text
    assert auth_client.delete(f"{PROJECTS}/{DEFAULT}").status_code == 409


def test_a_null_name_is_refused(auth_client):
    """A project always has a name."""
    project_id = create_project(auth_client, "Named")["project_id"]
    assert auth_client.patch(f"{PROJECTS}/{project_id}", json={"name": None}).status_code == 422


def test_a_workspace_moves_between_projects_without_touching_anything_else(auth_client, workspace):
    """A move writes `project_id` alone, and moving back to the default removes it."""
    workspace_id = workspace["workspace_id"]
    project_id = create_project(auth_client, "CarModPicker")["project_id"]

    moved = auth_client.patch(f"{WORKSPACES}/{workspace_id}", json={"project_id": project_id})
    assert moved.status_code == 200, moved.text
    assert moved.json()["project_id"] == project_id
    assert moved.json()["run_role_arn"] == workspace["run_role_arn"]
    assert auth_client.get(f"{PROJECTS}/{project_id}").json()["workspace_count"] == 1

    back = auth_client.patch(f"{WORKSPACES}/{workspace_id}", json={"project_id": DEFAULT})
    assert back.status_code == 200, back.text
    assert back.json()["project_id"] == DEFAULT
    stored = repositories.workspaces().get({"workspace_id": workspace_id})
    assert stored is not None
    assert "project_id" not in stored


def test_a_workspace_is_created_in_a_project(auth_client):
    """A create may name the project the workspace starts in."""
    project_id = create_project(auth_client, "Portfolio")["project_id"]
    created = auth_client.post(WORKSPACES, json={**WORKSPACE_PAYLOAD, "project_id": project_id})
    assert created.status_code == 201, created.text
    assert created.json()["project_id"] == project_id


def test_a_project_that_does_not_exist_is_refused(auth_client, workspace):
    """A create or a move into an unknown project is a 422 with a stable code."""
    missing = "prj-" + "0" * 26
    created = auth_client.post(WORKSPACES, json={**WORKSPACE_PAYLOAD, "name": "other", "project_id": missing})
    assert created.status_code == 422
    assert "PROJECT_NOT_FOUND" in created.text
    moved = auth_client.patch(f"{WORKSPACES}/{workspace['workspace_id']}", json={"project_id": missing})
    assert moved.status_code == 422
    assert "PROJECT_NOT_FOUND" in moved.text


def test_a_project_holding_workspaces_cannot_be_deleted(auth_client, workspace):
    """Deleting a project never orphans or moves its workspaces."""
    project_id = create_project(auth_client, "Artifacts")["project_id"]
    auth_client.patch(f"{WORKSPACES}/{workspace['workspace_id']}", json={"project_id": project_id})
    refused = auth_client.delete(f"{PROJECTS}/{project_id}")
    assert refused.status_code == 409
    assert "PROJECT_NOT_EMPTY" in refused.text


def test_the_workspace_list_filters_by_project(auth_client, workspace):
    """`project_id` keeps one project's workspaces, the default included."""
    project_id = create_project(auth_client, "Terraform")["project_id"]
    other = auth_client.post(WORKSPACES, json={**WORKSPACE_PAYLOAD, "name": "other", "project_id": project_id})
    assert other.status_code == 201, other.text

    in_project = auth_client.get(WORKSPACES, params={"project_id": project_id}).json()["items"]
    in_default = auth_client.get(WORKSPACES, params={"project_id": DEFAULT}).json()["items"]

    assert [item["name"] for item in in_project] == ["other"]
    assert [item["name"] for item in in_default] == ["example"]
