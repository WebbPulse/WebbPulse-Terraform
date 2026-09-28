"""Resolving the repository a workspace connects to, through the environment's GitHub App.

The resolution itself is shared in `app.common.github.repositories`, since registry
modules connect to repositories the same way. This module keeps the workspaces
domain's seam on the HTTP client. An environment with no App keeps the name only
binding, and the first upload records the id as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ...common.composition.settings import Settings
from ...common.github import repositories
from ...common.github.repositories import RepositoryNotInstalled, ResolvedRepository

if TYPE_CHECKING:  # pragma: no cover
    import httpx


def http_client() -> httpx.Client | None:
    """The HTTP client GitHub calls go through; `None` lets the client build its own.

    The seam the tests replace with a mock transport.
    """
    return None


def resolve_repository(repository: str, *, settings: Settings) -> Optional[ResolvedRepository]:
    """The repository as the App sees it, or `None` when the environment has no App.

    Raises `RepositoryNotInstalled` when no installation of the App covers it, and
    lets any other `GitHubError` through for the router to report as unavailable.
    """
    return repositories.resolve_repository(repository, settings=settings, client=http_client())


__all__ = ["RepositoryNotInstalled", "ResolvedRepository", "http_client", "resolve_repository"]
