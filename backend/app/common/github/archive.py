"""Reading a repository's tags and archives through the App's installation token.

`list_tags` pages `GET /repos/{repo}/tags`, which names each tag's commit even for
an annotated tag. GitHub answers `GET /repos/{repo}/tarball/{ref}` with a redirect to a signed
codeload URL. The installation token goes only to the API, the redirect is
followed without it, and only to codeload, so a token never reaches another host
and an archive never comes from one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx
from webbpulse.integrations.github import API_ROOT, API_VERSION, GitHubAppClient

TARBALL_HOSTS: Final = frozenset({"codeload.github.com"})
"""Where GitHub's tarball redirect may point. Anything else is refused."""

REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})


TAGS_PAGE_SIZE: Final = 100


class TarballUnavailable(Exception):
    """GitHub did not hand over an archive within the size limit, so the caller retries."""


class TagsUnavailable(Exception):
    """GitHub did not list the repository's tags, so the caller retries."""


def _api_headers(token: str) -> dict[str, str]:
    """The headers an installation token call to the REST API carries."""
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "Authorization": f"Bearer {token}",
    }


def list_tags(
    app: GitHubAppClient,
    http: httpx.Client,
    *,
    installation_id: int | str,
    repository: str,
    max_pages: int,
) -> list[tuple[str, str]]:
    """Each tag of `repository` as `(name, commit sha)`, reading at most `max_pages` pages.

    Raises:
        TagsUnavailable: The API refused or did not answer.
    """
    token = app.installation_token(installation_id)
    path = f"/repos/{repository}/tags"
    found: list[tuple[str, str]] = []
    for page in range(1, max_pages + 1):
        try:
            response = http.get(
                f"{API_ROOT}{path}",
                params={"per_page": TAGS_PAGE_SIZE, "page": page},
                headers=_api_headers(token),
            )
        except httpx.HTTPError as error:
            raise TagsUnavailable(f"GET {path} did not answer") from error
        if response.status_code != 200:
            raise TagsUnavailable(f"GET {path} answered {response.status_code}")
        body = response.json()
        if not isinstance(body, list):
            raise TagsUnavailable(f"GET {path} answered something other than a list")
        for item in body:
            name = str(item.get("name") or "") if isinstance(item, dict) else ""
            commit = item.get("commit") if isinstance(item, dict) else None
            sha = str(commit.get("sha") or "") if isinstance(commit, dict) else ""
            if name and sha:
                found.append((name, sha))
        if len(body) < TAGS_PAGE_SIZE:
            break
    return found


def download_tarball(
    app: GitHubAppClient,
    http: httpx.Client,
    *,
    installation_id: int | str,
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
    token = app.installation_token(installation_id)
    path = f"/repos/{repository}/tarball/{ref}"
    try:
        response = http.get(
            f"{API_ROOT}{path}",
            headers=_api_headers(token),
        )
    except httpx.HTTPError as error:
        raise TarballUnavailable(f"GET {path} did not answer") from error
    location = response.headers.get("location", "")
    if response.status_code not in REDIRECTS or not location:
        raise TarballUnavailable(f"GET {path} answered {response.status_code}")
    parts = urlsplit(location)
    if parts.scheme != "https" or parts.hostname not in TARBALL_HOSTS:
        raise TarballUnavailable("the archive redirect points somewhere unexpected")
    size = 0
    try:
        with http.stream("GET", location) as archive, target.open("wb") as handle:
            if archive.status_code != 200:
                raise TarballUnavailable(f"the archive answered {archive.status_code}")
            for chunk in archive.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise TarballUnavailable(f"the archive is larger than {max_bytes} bytes")
                handle.write(chunk)
    except httpx.HTTPError as error:
        raise TarballUnavailable("the archive download failed") from error
    return size


__all__ = ["TAGS_PAGE_SIZE", "TARBALL_HOSTS", "TagsUnavailable", "TarballUnavailable", "download_tarball", "list_tags"]
