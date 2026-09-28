"""Reading a repository's releases and their assets through the App's installation token.

`list_releases` pages `GET /repos/{repo}/releases` and `get_release` reads one by
tag. An asset is fetched from `GET /repos/{repo}/releases/assets/{id}` asking for
`application/octet-stream`, which GitHub answers with a redirect to a signed
download host. As for archives, the installation token goes only to the API and the
redirect is followed without it, and only to GitHub's own asset hosts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx
from webbpulse.integrations.github import API_ROOT, API_VERSION, GitHubAppClient

ASSET_HOSTS: Final = frozenset(
    {"objects.githubusercontent.com", "release-assets.githubusercontent.com", "github-releases.githubusercontent.com"}
)
"""Where GitHub's asset redirect may point. Anything else is refused."""

REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
RELEASES_PAGE_SIZE: Final = 100
CHUNK_BYTES: Final = 1024 * 1024


class ReleaseUnavailable(Exception):
    """GitHub did not answer about the release or hand over an asset, so the caller retries."""


class ReleaseNotFound(Exception):
    """The repository has no published release at the tag."""


def _api_headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    """The headers an installation token call to the REST API carries."""
    return {"Accept": accept, "X-GitHub-Api-Version": API_VERSION, "Authorization": f"Bearer {token}"}


def list_releases(
    app: GitHubAppClient, http: httpx.Client, *, installation_id: int | str, repository: str, max_pages: int
) -> list[dict[str, Any]]:
    """Each release of `repository`, newest first, reading at most `max_pages` pages.

    Raises:
        ReleaseUnavailable: The API refused or did not answer.
    """
    token = app.installation_token(installation_id)
    path = f"/repos/{repository}/releases"
    found: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        try:
            response = http.get(
                f"{API_ROOT}{path}", params={"per_page": RELEASES_PAGE_SIZE, "page": page}, headers=_api_headers(token)
            )
        except httpx.HTTPError as error:
            raise ReleaseUnavailable(f"GET {path} did not answer") from error
        if response.status_code != 200:
            raise ReleaseUnavailable(f"GET {path} answered {response.status_code}")
        body = response.json()
        if not isinstance(body, list):
            raise ReleaseUnavailable(f"GET {path} answered something other than a list")
        found.extend(item for item in body if isinstance(item, dict))
        if len(body) < RELEASES_PAGE_SIZE:
            break
    return found


def get_release(
    app: GitHubAppClient, http: httpx.Client, *, installation_id: int | str, repository: str, tag: str
) -> dict[str, Any]:
    """The published release of `repository` at `tag`.

    Raises:
        ReleaseNotFound: There is no such release, or it is still a draft.
        ReleaseUnavailable: The API refused or did not answer.
    """
    token = app.installation_token(installation_id)
    path = f"/repos/{repository}/releases/tags/{quote(tag, safe='')}"
    try:
        response = http.get(f"{API_ROOT}{path}", headers=_api_headers(token))
    except httpx.HTTPError as error:
        raise ReleaseUnavailable(f"GET {path} did not answer") from error
    if response.status_code == 404:
        raise ReleaseNotFound(tag)
    if response.status_code != 200:
        raise ReleaseUnavailable(f"GET {path} answered {response.status_code}")
    body = response.json()
    if not isinstance(body, dict) or body.get("draft"):
        raise ReleaseNotFound(tag)
    return body


def download_asset(
    app: GitHubAppClient,
    http: httpx.Client,
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
    token = app.installation_token(installation_id)
    path = f"/repos/{repository}/releases/assets/{asset_id}"
    try:
        response = http.get(f"{API_ROOT}{path}", headers=_api_headers(token, "application/octet-stream"))
    except httpx.HTTPError as error:
        raise ReleaseUnavailable(f"GET {path} did not answer") from error
    location = response.headers.get("location", "")
    if response.status_code not in REDIRECTS or not location:
        raise ReleaseUnavailable(f"GET {path} answered {response.status_code}")
    parts = urlsplit(location)
    if parts.scheme != "https" or parts.hostname not in ASSET_HOSTS:
        raise ReleaseUnavailable("the asset redirect points somewhere unexpected")
    digest = hashlib.sha256()
    size = 0
    try:
        with http.stream("GET", location) as asset, target.open("wb") as handle:
            if asset.status_code != 200:
                raise ReleaseUnavailable(f"the asset answered {asset.status_code}")
            for chunk in asset.iter_bytes(CHUNK_BYTES):
                size += len(chunk)
                if size > max_bytes:
                    raise ReleaseUnavailable(f"the asset is larger than {max_bytes} bytes")
                digest.update(chunk)
                handle.write(chunk)
    except httpx.HTTPError as error:
        raise ReleaseUnavailable("the asset download failed") from error
    return digest.hexdigest()


__all__ = [
    "ASSET_HOSTS",
    "RELEASES_PAGE_SIZE",
    "ReleaseNotFound",
    "ReleaseUnavailable",
    "download_asset",
    "get_release",
    "list_releases",
]
