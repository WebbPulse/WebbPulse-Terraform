"""Shared pieces for the ingest consumer, webhook consumer and reporting suites.

`claims` and `pr_claims` describe one push or pull request the way an ingest record
is keyed, and `tarball` builds the repacked archive a delivery leaves under `ingest/`.
"""

import io
import tarfile
from typing import Any

REPO = "WebbPulse/example-infra"
REPOSITORY_ID = "424242"
HEAD_SHA = "a" * 40
MERGE_SHA = "b" * 40
BASE_SHA = "c" * 40


def claims(**overrides: Any) -> dict[str, Any]:
    """One push's attributes, with `overrides` applied."""
    base = {
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
    """One pull request's attributes."""
    return claims(**({"event_name": "pull_request", "ref": f"refs/pull/{number}/merge", "sha": MERGE_SHA} | overrides))


def tarball(changed: str | None = "main.tf\n", files: dict[str, str] | None = None) -> bytes:
    """A gzipped tarball shaped like a repacked delivery, rooted at `./`."""
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
