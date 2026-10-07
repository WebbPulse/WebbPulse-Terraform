"""Project storage: the groups workspaces are organised into, like HCP Terraform's projects.

A project's name is unique ignoring case, claimed the way a workspace name is: a read
of the `by_name` index refuses a duplicate, and the conditional put on the id keeps two
rows from sharing one. The default project is never stored, so it cannot be renamed or
deleted and every workspace without a `project_id` is in it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.db.tables import PROJECTS_BY_NAME_INDEX
from ...common.workspaces.projects import (
    DEFAULT_PROJECT_ID,
    DEFAULT_PROJECT_NAME,
    PROJECT_ID_PREFIX,
    project_of,
)


class ProjectNotFound(Exception):
    """No project with this id."""


class ProjectNameTaken(Exception):
    """Another project already holds this name, ignoring case."""


class ProjectNotEmpty(Exception):
    """The project still holds workspaces, so deleting it would orphan them."""


class DefaultProjectReadOnly(Exception):
    """The default project is not stored, so it can be neither edited nor deleted."""


def name_key(name: str) -> str:
    """The form a project name is compared in for uniqueness."""
    return name.casefold()


def _default_project() -> dict[str, Any]:
    """The default project as a row, which is never written."""
    return {"project_id": DEFAULT_PROJECT_ID, "name": DEFAULT_PROJECT_NAME, "description": ""}


def render_project(item: dict[str, Any], *, workspace_count: int = 0) -> dict[str, Any]:
    """One project as the API returns it."""
    project_id = str(item["project_id"])
    return {
        "project_id": project_id,
        "name": str(item.get("name", "")),
        "description": str(item.get("description", "") or ""),
        "is_default": project_id == DEFAULT_PROJECT_ID,
        "workspace_count": workspace_count,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _workspace_counts(settings: Settings) -> Counter[str]:
    """How many workspaces each project holds, from one scan of the workspaces table."""
    return Counter(project_of(row) for row in repositories.workspaces(settings).iter_scan())


def find_by_name(name: str, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """The stored project holding this name ignoring case, or `None`."""
    resolved = settings or get_settings()
    page = repositories.projects(resolved).query(
        Key("name_key").eq(name_key(name)),
        index_name=PROJECTS_BY_NAME_INDEX,
        limit=1,
    )
    return page.items[0] if page.items else None


def _name_taken(name: str, *, except_id: str | None, settings: Settings) -> bool:
    """Whether a project other than `except_id` holds this name, the default's included."""
    if name_key(name) == name_key(DEFAULT_PROJECT_NAME):
        return True
    found = find_by_name(name, settings=settings)
    return found is not None and str(found["project_id"]) != except_id


def get_project_row(project_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One project row by id, the default included, or `ProjectNotFound`."""
    if project_id == DEFAULT_PROJECT_ID:
        return _default_project()
    resolved = settings or get_settings()
    item = repositories.projects(resolved).get({"project_id": project_id})
    if item is None:
        raise ProjectNotFound(project_id)
    return item


def require_project(project_id: str, *, settings: Settings | None = None) -> None:
    """Refuse a workspace move into a project that does not exist."""
    get_project_row(project_id, settings=settings)


def get_project(project_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One project rendered with its workspace count, or `ProjectNotFound`."""
    resolved = settings or get_settings()
    item = get_project_row(project_id, settings=resolved)
    return render_project(item, workspace_count=_workspace_counts(resolved)[project_id])


def list_projects(*, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every project, the default first and the rest by name ignoring case.

    A scan, since an environment holds a handful of projects and the table has no
    collection key to query on.
    """
    resolved = settings or get_settings()
    stored = sorted(
        repositories.projects(resolved).iter_scan(),
        key=lambda item: (name_key(str(item.get("name", ""))), str(item["project_id"])),
    )
    counts = _workspace_counts(resolved)
    return [
        render_project(item, workspace_count=counts[str(item["project_id"])])
        for item in (_default_project(), *stored)
    ]


def create_project(payload: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Store a new project, refusing a name another project holds."""
    resolved = settings or get_settings()
    name = str(payload["name"])
    if _name_taken(name, except_id=None, settings=resolved):
        raise ProjectNameTaken(name)
    now = now_iso()
    item: dict[str, Any] = {
        "project_id": f"{PROJECT_ID_PREFIX}{new_ulid()}",
        "name": name,
        "name_key": name_key(name),
        "description": str(payload.get("description", "") or ""),
        "created_at": now,
        "updated_at": now,
    }
    try:
        repositories.projects(resolved).put(item, condition=Attr("project_id").not_exists())
    except ConditionFailed as error:
        raise ProjectNameTaken(name) from error
    return render_project(item)


def update_project(
    project_id: str,
    changes: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Rename a project or change its description. A null description clears it."""
    resolved = settings or get_settings()
    if project_id == DEFAULT_PROJECT_ID:
        raise DefaultProjectReadOnly(project_id)
    get_project_row(project_id, settings=resolved)
    assignments: dict[str, Any] = {"updated_at": now_iso()}
    if changes.get("name") is not None:
        name = str(changes["name"])
        if _name_taken(name, except_id=project_id, settings=resolved):
            raise ProjectNameTaken(name)
        assignments |= {"name": name, "name_key": name_key(name)}
    if "description" in changes:
        assignments["description"] = str(changes["description"] or "")
    names = {f"#{key}": key for key in assignments}
    values = {f":{key}": value for key, value in assignments.items()}
    try:
        updated = repositories.projects(resolved).update(
            {"project_id": project_id},
            update_expression="SET " + ", ".join(f"#{key} = :{key}" for key in assignments),
            expression_names=names,
            expression_values=values,
            condition=Attr("project_id").exists(),
            return_values="ALL_NEW",
        )
    except ConditionFailed as error:
        raise ProjectNotFound(project_id) from error
    if updated is None:
        raise ProjectNotFound(project_id)
    return render_project(updated, workspace_count=_workspace_counts(resolved)[project_id])


def delete_project(project_id: str, *, settings: Settings | None = None) -> None:
    """Delete an empty project. One that still holds workspaces is refused, never emptied."""
    resolved = settings or get_settings()
    if project_id == DEFAULT_PROJECT_ID:
        raise DefaultProjectReadOnly(project_id)
    get_project_row(project_id, settings=resolved)
    if _workspace_counts(resolved)[project_id]:
        raise ProjectNotEmpty(project_id)
    repositories.projects(resolved).delete({"project_id": project_id})
