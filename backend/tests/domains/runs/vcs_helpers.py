"""Shared pieces for the VCS upload route and ingest consumer suites.

GitHub's signing keys cannot be reached from the suite, so the verifier is swapped
for one that accepts a token naming a known claim set and rejects anything else
with the same `InvalidToken` the real one raises.
"""

import io
import json
import tarfile
from typing import Any

from webbpulse.identity.service import InvalidToken

REPO = "WebbPulse/example-infra"
REPOSITORY_ID = "424242"
HEAD_SHA = "a" * 40
MERGE_SHA = "b" * 40
BASE_SHA = "c" * 40


def claims(**overrides: Any) -> dict[str, Any]:
    """A verified push token's claims, with `overrides` applied."""
    base = {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": "webbpulse-terraform",
        "sub": f"repo:{REPO}:ref:refs/heads/main",
        "repository": REPO,
        "repository_id": REPOSITORY_ID,
        "repository_owner": "WebbPulse",
        "event_name": "push",
        "ref": "refs/heads/main",
        "sha": HEAD_SHA,
        "actor": "octocat",
        "run_id": "9000",
        "run_attempt": "1",
    }
    return base | overrides


def pr_claims(number: int = 7, **overrides: Any) -> dict[str, Any]:
    """A verified pull request token's claims."""
    return claims(**({"event_name": "pull_request", "ref": f"refs/pull/{number}/merge", "sha": MERGE_SHA} | overrides))


def token_for(values: dict[str, Any]) -> str:
    """The bearer the fake verifier accepts for `values`."""
    return "fake." + json.dumps(values, sort_keys=True)


class FakeVerifier:
    """Accepts `token_for` tokens and rejects every other string."""

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, token: str, *, expected_type: str | None = None) -> dict[str, Any]:
        """The claims the token names, or `InvalidToken`."""
        self.calls += 1
        if not token.startswith("fake."):
            raise InvalidToken("signature verification failed")
        return json.loads(token.removeprefix("fake."))


def tarball(changed: str | None = "main.tf\n", files: dict[str, str] | None = None) -> bytes:
    """A gzipped tarball shaped like the workflow's, rooted at `./`."""
    contents = {"main.tf": "terraform {}\n"} | (files or {})
    if changed is not None:
        contents[".webbpulse/changed-paths.txt"] = changed
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in sorted(contents):
            data = contents[name].encode()
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
