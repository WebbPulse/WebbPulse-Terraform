"""Fetching a repository archive through the App's installation token.

GitHub answers `GET /repos/{repo}/tarball/{ref}` with a redirect to a signed
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


class TarballUnavailable(Exception):
    """GitHub did not hand over an archive within the size limit, so the caller retries."""


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
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {token}",
            },
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


__all__ = ["TARBALL_HOSTS", "TarballUnavailable", "download_tarball"]
