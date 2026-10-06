"""Reading a repository's releases and their assets through the App's installation.

Every call goes through `GitHubAppClient`, which retries a transient failure, sends
the installation token only to the API and follows an asset redirect without it,
and only to GitHub's own asset hosts. A missing or draft release is
`ReleaseNotFound`; any other failure is the retryable `ReleaseUnavailable`.
"""

from __future__ import annotations

from pathlib import Path

from webbpulse.integrations.github import GitHubAppClient, GitHubError, GitHubNotFound, Release


class ReleaseUnavailable(Exception):
    """GitHub did not answer about the release or hand over an asset, so the caller retries."""


class ReleaseNotFound(Exception):
    """The repository has no published release at the tag."""


def list_releases(
    app: GitHubAppClient, *, installation_id: int | str, repository: str, max_pages: int
) -> list[Release]:
    """Each release of `repository`, newest first, reading at most `max_pages` pages.

    Raises:
        ReleaseUnavailable: The API refused or did not answer.
    """
    try:
        return app.list_releases(repository, max_pages=max_pages, installation_id=installation_id)
    except (GitHubError, ValueError) as error:
        raise ReleaseUnavailable(f"listing the releases of {repository} failed: {error}") from error


def get_release(app: GitHubAppClient, *, installation_id: int | str, repository: str, tag: str) -> Release:
    """The published release of `repository` at `tag`.

    Raises:
        ReleaseNotFound: There is no such release, or it is still a draft.
        ReleaseUnavailable: The API refused or did not answer.
    """
    try:
        release = app.get_release_by_tag(repository, tag, installation_id=installation_id)
    except (GitHubNotFound, ValueError) as error:
        raise ReleaseNotFound(tag) from error
    except GitHubError as error:
        raise ReleaseUnavailable(f"reading the release {tag} of {repository} failed: {error}") from error
    if release.draft:
        raise ReleaseNotFound(tag)
    return release


def download_asset(
    app: GitHubAppClient,
    *,
    installation_id: int | str,
    repository: str,
    asset_id: int | str,
    target: Path,
    max_bytes: int,
) -> str:
    """Fetch one release asset into `target`, returning its hex SHA-256.

    Raises:
        ReleaseUnavailable: The API or the asset host refused, the redirect pointed
            somewhere unexpected, or the asset outgrew `max_bytes`.
    """
    try:
        download = app.download_release_asset(
            repository, asset_id, target, max_bytes=max_bytes, installation_id=installation_id
        )
    except (GitHubError, ValueError) as error:
        raise ReleaseUnavailable(f"fetching asset {asset_id} of {repository} failed: {error}") from error
    return download.sha256


__all__ = ["ReleaseNotFound", "ReleaseUnavailable", "download_asset", "get_release", "list_releases"]
