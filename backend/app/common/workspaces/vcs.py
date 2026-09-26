"""Finding the workspaces bound to a GitHub repository.

A workspace binds a repository by `vcs_repo`, an `owner/name` a person typed, and
records the repository's GitHub id the first time an upload from it arrives. From
then on the id is what matches, so a renamed or transferred repository keeps its
binding and a new repository that takes over the old name does not inherit it.

Both deployed functions read through here. The upload route decides whether to
issue a URL at all, and the ingest consumer decides which workspaces an upload
starts runs on.
"""

from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed

from ..composition.settings import Settings, get_settings
from ..db import repositories
from ..db.tables import WORKSPACES_BY_VCS_REPO_INDEX, WORKSPACES_BY_VCS_REPOSITORY_ID_INDEX


def repo_key(repository: str) -> str:
    """The lowercased `owner/name` the binding index is keyed on. GitHub names are
    case insensitive."""
    return repository.strip().lower()


def bound_workspaces(repository: str, repository_id: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every workspace bound to this repository, oldest first.

    A workspace matches when its recorded `vcs_repository_id` is this id, or when
    it has recorded none yet and its `vcs_repo` names this repository. A workspace
    whose recorded id is a different repository's does not match, whatever its
    `vcs_repo` says.
    """
    resolved = settings or get_settings()
    table = repositories.workspaces(resolved)
    found: dict[str, dict[str, Any]] = {}
    for item in table.iter_query(
        Key("vcs_repository_id").eq(str(repository_id)),
        index_name=WORKSPACES_BY_VCS_REPOSITORY_ID_INDEX,
    ):
        found[str(item["workspace_id"])] = item
    for item in table.iter_query(
        Key("vcs_repo_key").eq(repo_key(repository)),
        index_name=WORKSPACES_BY_VCS_REPO_INDEX,
    ):
        recorded = item.get("vcs_repository_id")
        if recorded is None or str(recorded) == str(repository_id):
            found[str(item["workspace_id"])] = item
    return [found[key] for key in sorted(found)]


def record_repository_id(workspace_id: str, repository_id: str, *, settings: Settings | None = None) -> bool:
    """Record the repository id on a bound workspace that has none yet.

    Conditional on the attribute still being absent, so a concurrent upload or a
    rebind in between is never overwritten. Returns whether this call wrote it.
    """
    resolved = settings or get_settings()
    try:
        repositories.workspaces(resolved).update(
            {"workspace_id": workspace_id},
            update_expression="SET #id = :id",
            expression_names={"#id": "vcs_repository_id"},
            expression_values={":id": str(repository_id)},
            condition=Attr("workspace_id").exists() & Attr("vcs_repository_id").not_exists(),
        )
    except ConditionFailed:
        return False
    return True


__all__ = ["bound_workspaces", "record_repository_id", "repo_key"]
