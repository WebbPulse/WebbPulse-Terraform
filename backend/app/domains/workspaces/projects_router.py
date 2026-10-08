"""The project routes: list, read, create, edit and delete the groups workspaces sit in.

Projects ride on the workspace scopes, as HCP Terraform's do on workspace management:
reading one takes `workspaces:read` and changing one takes `workspaces:write`.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Path, status

from ...common.core.auth import WORKSPACES_READ, WORKSPACES_WRITE, run_api_workspace_binding, scopes
from ...common.workspaces.projects import PROJECT_ID_PATTERN
from . import projects
from .schemas.project import Project, ProjectCreate, ProjectList, ProjectUpdate

PROJECT_NAME_TAKEN_CODE: Final = "PROJECT_NAME_TAKEN"
"""The stable code a create or rename refuses with when the name is held, ignoring case."""

PROJECT_NOT_EMPTY_CODE: Final = "PROJECT_NOT_EMPTY"
"""The stable code a delete refuses with while the project still holds workspaces."""

DEFAULT_PROJECT_READ_ONLY_CODE: Final = "DEFAULT_PROJECT_READ_ONLY"
"""The stable code an edit or delete of the default project is refused with."""

router = APIRouter(dependencies=[Depends(run_api_workspace_binding)])

ProjectId = Path(min_length=4, max_length=64, pattern=PROJECT_ID_PATTERN)


def _not_found() -> HTTPException:
    """The 404 an absent project raises."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such project.")


def _conflict(message: str, code: str) -> HTTPException:
    """A 409 in the shared error envelope's shape, carrying a stable code."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"message": message, "error_code": code})


def _default_read_only() -> HTTPException:
    """The 409 any change to the default project raises."""
    return _conflict("The default project cannot be changed or deleted.", DEFAULT_PROJECT_READ_ONLY_CODE)


def _name_taken(name: str) -> HTTPException:
    """The 409 a duplicate project name raises."""
    return _conflict(f"A project named '{name}' already exists.", PROJECT_NAME_TAKEN_CODE)


@router.get("/projects", response_model=ProjectList, dependencies=[Depends(scopes(WORKSPACES_READ))])
def list_projects() -> dict[str, Any]:
    """Every project with its workspace count, the default project first."""
    return {"items": projects.list_projects()}


@router.post(
    "/projects",
    response_model=Project,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def create_project(payload: ProjectCreate) -> dict[str, Any]:
    """Create a project. The name has to be free, ignoring case."""
    try:
        return projects.create_project(payload.model_dump())
    except projects.ProjectNameTaken as error:
        raise _name_taken(str(error)) from error


@router.get("/projects/{project_id}", response_model=Project, dependencies=[Depends(scopes(WORKSPACES_READ))])
def get_project(project_id: str = ProjectId) -> dict[str, Any]:
    """One project with its workspace count. `prj-default` is the default project."""
    try:
        return projects.get_project(project_id)
    except projects.ProjectNotFound as error:
        raise _not_found() from error


@router.patch("/projects/{project_id}", response_model=Project, dependencies=[Depends(scopes(WORKSPACES_WRITE))])
def update_project(payload: ProjectUpdate, project_id: str = ProjectId) -> dict[str, Any]:
    """Rename a project or change its description. The default project is refused."""
    try:
        return projects.update_project(project_id, payload.model_dump(exclude_unset=True))
    except projects.ProjectNotFound as error:
        raise _not_found() from error
    except projects.DefaultProjectReadOnly as error:
        raise _default_read_only() from error
    except projects.ProjectNameTaken as error:
        raise _name_taken(str(error)) from error


@router.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def delete_project(project_id: str = ProjectId) -> None:
    """Delete an empty project. Move its workspaces out first; nothing is moved for you."""
    try:
        projects.delete_project(project_id)
    except projects.ProjectNotFound as error:
        raise _not_found() from error
    except projects.DefaultProjectReadOnly as error:
        raise _default_read_only() from error
    except projects.ProjectNotEmpty as error:
        raise _conflict("This project still holds workspaces. Move them first.", PROJECT_NOT_EMPTY_CODE) from error
