"""The runner's half of the runs domain API and the presigned artifact transfers."""

from __future__ import annotations

from pathlib import Path

import httpx

from app.models import Bundle, PhaseResult, RunnerEnv

DEFAULT_TIMEOUT = httpx.Timeout(30.0, read=120.0)


class ApiError(RuntimeError):
    """The runs domain rejected a call or could not be reached."""


class RunnerApi:
    """Fetches the bundle, moves artifacts over presigned URLs and posts the result."""

    def __init__(self, env: RunnerEnv, client: httpx.Client) -> None:
        self._env = env
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._env.run_token.get_secret_value()}"}

    def fetch_bundle(self) -> Bundle:
        """Read the phase bundle, raising `ApiError` on any non success response."""
        url = f"{self._env.api_base_url}/api/v1/runs/{self._env.run_id}/bundle"
        try:
            response = self._client.get(url, headers=self._headers)
        except httpx.HTTPError as error:
            raise ApiError(f"bundle fetch failed: {type(error).__name__}") from error
        if response.status_code != 200:
            raise ApiError(f"bundle fetch returned {response.status_code}")
        try:
            return Bundle.model_validate(response.json())
        except ValueError as error:
            raise ApiError("bundle payload was not a valid bundle") from error

    def post_phase_result(self, result: PhaseResult) -> None:
        """Report the phase outcome, raising `ApiError` when the API rejects it."""
        url = f"{self._env.api_base_url}/api/v1/runs/{self._env.run_id}/phase-result"
        try:
            response = self._client.post(url, headers=self._headers, json=result.model_dump(mode="json"))
        except httpx.HTTPError as error:
            raise ApiError(f"phase result post failed: {type(error).__name__}") from error
        if response.status_code >= 400:
            raise ApiError(f"phase result post returned {response.status_code}")

    def download(self, url: str, destination: Path) -> Path:
        """Stream a presigned GET to disk."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ApiError(f"download returned {response.status_code}")
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as error:
            raise ApiError(f"download failed: {type(error).__name__}") from error
        return destination

    def upload_file(self, url: str | None, source: Path, content_type: str) -> bool:
        """Put a file to a presigned URL, reporting whether it was sent."""
        if not url or not source.exists():
            return False
        try:
            response = self._client.put(url, content=source.read_bytes(), headers={"Content-Type": content_type})
        except httpx.HTTPError as error:
            raise ApiError(f"upload failed: {type(error).__name__}") from error
        if response.status_code >= 400:
            raise ApiError(f"upload returned {response.status_code}")
        return True

    def upload_text(self, url: str | None, body: str, content_type: str) -> bool:
        """Put an in memory document to a presigned URL, reporting whether it was sent."""
        if not url:
            return False
        try:
            response = self._client.put(url, content=body.encode(), headers={"Content-Type": content_type})
        except httpx.HTTPError as error:
            raise ApiError(f"upload failed: {type(error).__name__}") from error
        if response.status_code >= 400:
            raise ApiError(f"upload returned {response.status_code}")
        return True


def build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An httpx client with the runner's timeouts, or one wired to a test transport."""
    if transport is not None:
        return httpx.Client(transport=transport, timeout=DEFAULT_TIMEOUT)
    return httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
