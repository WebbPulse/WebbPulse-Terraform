"""Reading a repository's tags and archives through the App's installation.

Both go through `GitHubAppClient`, which retries a transient failure, sends the
installation token only to the API and follows the archive redirect without it, and
only to codeload. A failure either way comes back as this module's retryable error.
"""

from __future__ import annotations

from pathlib import Path

from webbpulse.integrations.github import GitHubAppClient, GitHubError


class TarballUnavailable(Exception):
    """GitHub did not hand over an archive within the size limit, so the caller retries."""


class TagsUnavailable(Exception):
    """GitHub did not list the repository's tags, so the caller retries."""


def list_tags(
    app: GitHubAppClient,
    *,
    installation_id: int | str,
    repository: str,
    max_pages: int,
) -> list[tuple[str, str]]:
    """Each tag of `repository` as `(name, commit sha)`, reading at most `max_pages` pages.

    Raises:
        TagsUnavailable: The API refused or did not answer.
    """
    try:
        listed = app.list_tags(repository, max_pages=max_pages, installation_id=installation_id)
    except (GitHubError, ValueError) as error:
        raise TagsUnavailable(f"listing the tags of {repository} failed: {error}") from error
    return [(tag.name, tag.sha) for tag in listed]


def download_tarball(
    app: GitHubAppClient,
    *,
    installation_id: int | str | None,
    repository: str,
    ref: str,
    target: Path,
    max_bytes: int,
) -> int:
    """Fetch the archive of `repository` at `ref` into `target`, returning its size.

    Raises:
        TarballUnavailable: The API or codeload refused, the redirect pointed
            somewhere unexpected, or the archive outgrew `max_bytes`.
    """
    try:
        download = app.download_tarball(repository, ref, target, max_bytes=max_bytes, installation_id=installation_id)
    except (GitHubError, ValueError) as error:
        raise TarballUnavailable(f"fetching the archive of {repository} failed: {error}") from error
    return download.size


__all__ = ["TagsUnavailable", "TarballUnavailable", "download_tarball", "list_tags"]
