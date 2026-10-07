"""The project a workspace belongs to, read the same way by both deployed functions.

A workspace names its project in `project_id`. A workspace without that attribute
belongs to the default project, which is never stored, so every workspace created
before projects existed is in it with no write at all.
"""

from __future__ import annotations

from typing import Any, Final

from ..composition.settings import Settings, get_settings
from ..db import repositories

PROJECT_ID_PREFIX: Final = "prj-"

DEFAULT_PROJECT_ID: Final = "prj-default"
"""The project every workspace without a `project_id` belongs to."""

DEFAULT_PROJECT_NAME: Final = "Default Project"

PROJECT_ID_PATTERN: Final = r"^prj-(default|[0-9A-HJKMNP-TV-Z]{26})$"
"""A stored project's id, or the default project's."""


def project_of(item: dict[str, Any]) -> str:
    """The project one stored workspace row belongs to."""
    return str(item.get("project_id") or DEFAULT_PROJECT_ID)


def project_workspace_ids(project_id: str, *, settings: Settings | None = None) -> list[str]:
    """The ids of every workspace in one project, oldest first."""
    resolved = settings or get_settings()
    rows = repositories.workspaces(resolved).iter_scan()
    return sorted(str(row["workspace_id"]) for row in rows if project_of(row) == project_id)
