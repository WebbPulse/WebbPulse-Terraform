"""Resolving a repository by name through the environment's GitHub App.

A person picks a repository by name and the API resolves everything else. The App's
own JWT finds the installation covering the repository, and that installation's
token lists what it can see, which yields the repository id, its canonical name and
its default branch. Nothing outside the App's grant is readable, so a binding can
only name a repository the App was installed on.

Shared by every domain that binds to a repository: a workspace connecting to its
configuration, and a registry module connecting to the repository its tags publish.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from webbpulse.integrations.github import GitHubAppClient, GitHubNotConfigured, GitHubNotFound

from ..composition.settings import Settings
from .loader import github_app_settings

if TYPE_CHECKING:  # pragma: no cover
    import httpx


class RepositoryNotInstalled(Exception):
    """The environment's GitHub App is not installed on the repository."""


@dataclass(frozen=True, slots=True)
class ResolvedRepository:
    """What the App reports about a repository it can see."""

    full_name: str
    repository_id: str
    installation_id: str
    default_branch: Optional[str]


def resolve_repository(
    repository: str, *, settings: Settings, client: httpx.Client | None = None
) -> Optional[ResolvedRepository]:
    """The repository as the App sees it, or `None` when the environment has no App.

    `client` is the HTTP client the App's calls go through; `None` lets the App
    client build its own.

    Raises `RepositoryNotInstalled` when no installation of the App covers it, and
    lets any other `GitHubError` through for the caller to report as unavailable.
    """
    try:
        credentials = github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    except GitHubNotConfigured:
        return None
    key = repository.strip().lower()
    try:
        with GitHubAppClient.from_settings(credentials, client=client) as app:
            installation_id = app.repository_installation(repository)
            listed = app.list_installation_repositories(installation_id)
    except GitHubNotFound as error:
        raise RepositoryNotInstalled(repository) from error
    for item in listed:
        full_name = str(item.get("full_name") or "")
        repository_id = item.get("id")
        if full_name.lower() == key and repository_id:
            default_branch = item.get("default_branch")
            return ResolvedRepository(
                full_name=full_name,
                repository_id=str(repository_id),
                installation_id=str(installation_id),
                default_branch=str(default_branch) if default_branch else None,
            )
    raise RepositoryNotInstalled(repository)


__all__ = ["RepositoryNotInstalled", "ResolvedRepository", "resolve_repository"]
