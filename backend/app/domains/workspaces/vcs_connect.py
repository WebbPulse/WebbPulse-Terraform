"""Resolving the repository a workspace connects to, through the environment's GitHub App.

A person picks a repository by name and the API resolves everything else. The App's
own JWT finds the installation covering the repository, and that installation's
token lists what it can see, which yields the repository id, its canonical name and
its default branch. Nothing is typed by hand and nothing outside the App's grant is
readable, so the binding can only name a repository the App was installed on.

The credentials come from the `app` secret every domain function already reads, so
this needs no extra grant, no table of the GitHub domain and no call between functions.
An environment with no App keeps the name only binding, and the first upload records
the id as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from webbpulse.integrations.github import GitHubAppClient, GitHubNotConfigured, GitHubNotFound

from ...common.composition.settings import Settings
from ...common.github.loader import github_app_settings

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
    try:
        credentials = github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    except GitHubNotConfigured:
        return None
    key = repository.strip().lower()
    try:
        with GitHubAppClient.from_settings(credentials, client=http_client()) as client:
            installation_id = client.repository_installation(repository)
            listed = client.list_installation_repositories(installation_id)
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


__all__ = ["RepositoryNotInstalled", "ResolvedRepository", "http_client", "resolve_repository"]
