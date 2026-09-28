"""Importing a repository's existing tags into the registry, against the deployed staging stage.

The fixture is the `registry-backfill-fixture` branch of
`WebbPulse/webbpulse-terraform-staging-e2e`: one commit holding a root `main.tf`, tagged
`v0.2.0` and with the non-semver tag `registry-backfill-fixture`. Both tags predate every
module these cases connect, so no push event can publish them: a published `0.2.0` proves
the import. The repository's older `v0.1.0` tag keeps its module under a subdirectory, so
it is imported too and settles `failed`, which is expected and asserted.

Each case connects its own module under the run's prefix and deletes it on teardown,
whatever the outcome, after its versions settle so no queued tag outlives the module.
Skipped outside staging, since only the staging GitHub App is installed on the repository.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any, Callable

import pytest

REPOSITORY = "WebbPulse/webbpulse-terraform-staging-e2e"
NAMESPACE, PROVIDER = "WebbPulse", "null"
VERSION = "0.2.0"
FIXTURE_TAG = f"v{VERSION}"
FIXTURE_SHA = "ded16beffd7111ef571296af462b637df8353202"
UNPUBLISHABLE_VERSION = "0.1.0"
NON_SEMVER_TAG = "registry-backfill-fixture"
MODULES = "/api/v1/registry/modules"
SETTLE_TIMEOUT_SECONDS = 180
POLL_SECONDS = 5

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.xdist_group("registry"),
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="the backfill fixture repository has the staging GitHub App only",
    ),
]


def _module_name(prefix: str, label: str) -> str:
    """A registry-valid module name unique to this run and case."""
    raw = f"{prefix}{label}-{secrets.token_hex(3)}"
    return re.sub(r"[^0-9A-Za-z_-]", "-", raw).strip("-_")[:64].rstrip("-_")


def _path(name: str) -> str:
    """The module's route under the registry API."""
    return f"{MODULES}/{NAMESPACE}/{name}/{PROVIDER}"


def _versions(api: Any, name: str) -> dict[str, dict[str, Any]]:
    """The module's version rows by version."""
    response = api.get(_path(name))
    assert response.status_code == 200, f"reading {name} answered {response.status_code}: {response.text[:400]}"
    return {row["version"]: row for row in response.json()["versions"]}


def _wait_for(api: Any, name: str, done: Callable[[dict[str, dict[str, Any]]], bool]) -> dict[str, dict[str, Any]]:
    """Poll the module until `done` holds for its versions, failing with the last seen state."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    versions = _versions(api, name)
    while not done(versions):
        if time.monotonic() > deadline:
            seen = {version: row["status"] for version, row in versions.items()}
            pytest.fail(f"{name} did not settle within {SETTLE_TIMEOUT_SECONDS}s: {seen}")
        time.sleep(POLL_SECONDS)
        versions = _versions(api, name)
    return versions


def _settled(versions: dict[str, dict[str, Any]]) -> bool:
    """Both tagged versions imported and none still pending."""
    return {VERSION, UNPUBLISHABLE_VERSION} <= set(versions) and all(
        row["status"] != "pending" for row in versions.values()
    )


@pytest.fixture
def connect(api: Any) -> Iterator[Callable[..., dict[str, Any]]]:
    """Connect modules to the fixture repository, deleting each one on teardown."""
    connected: list[str] = []

    def _connect(name: str, *, import_tags: bool) -> dict[str, Any]:
        """Connect `name` and return the created module."""
        response = api.post(
            MODULES,
            json={"vcs_repo": REPOSITORY, "name": name, "provider": PROVIDER, "import_tags": import_tags},
        )
        assert response.status_code == 201, f"connecting {name} answered {response.status_code}: {response.text[:400]}"
        connected.append(name)
        return dict(response.json())

    yield _connect

    failures = []
    for name in connected:
        deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = api.get(_path(name))
            if response.status_code != 200 or all(row["status"] != "pending" for row in response.json()["versions"]):
                break
            time.sleep(POLL_SECONDS)
        response = api.delete(_path(name))
        if response.status_code not in (204, 404):
            failures.append(f"{name} ({response.status_code})")
    if failures:
        pytest.fail(f"e2e teardown could not delete the backfill modules: {', '.join(failures)}")


def _assert_imported(versions: dict[str, dict[str, Any]]) -> None:
    """The fixture tag published from its commit, the subdirectory tag failed, and nothing else was imported."""
    assert set(versions) == {VERSION, UNPUBLISHABLE_VERSION}, f"imported {sorted(versions)}"
    published = versions[VERSION]
    assert published["status"] == "published", f"{VERSION} is {published['status']}: {published.get('error')}"
    assert published["tag"] == FIXTURE_TAG
    assert published["sha"] == FIXTURE_SHA
    assert published["repository"].lower() == REPOSITORY.lower()
    assert published["size_bytes"], "the published version recorded no size"
    assert versions[UNPUBLISHABLE_VERSION]["status"] == "failed"
    assert NON_SEMVER_TAG not in {row.get("tag") for row in versions.values()}


def test_connecting_imports_the_existing_tags(api: Any, e2e_env: Any, connect: Callable[..., dict[str, Any]]) -> None:
    """A tag pushed before the module existed publishes on connect, with no push."""
    name = _module_name(e2e_env.resource_prefix, "backfill")
    module = connect(name, import_tags=True)
    assert module["vcs_repo"].lower() == REPOSITORY.lower()

    _assert_imported(_wait_for(api, name, _settled))
    _assert_documented(api, name)


def _assert_documented(api: Any, name: str) -> None:
    """The module page's read of the published version carries its readme and its one output."""
    response = api.get(f"{_path(name)}/versions/{VERSION}")
    assert response.status_code == 200, f"reading {VERSION} answered {response.status_code}: {response.text[:400]}"
    body = response.json()
    assert body["version"]["status"] == "published"
    docs = body["docs"]
    assert docs is not None, "the published version carries no documentation"
    assert docs["readme"], "the fixture's README.md was not read"
    assert [output["name"] for output in docs["outputs"]] == ["fixture"]
    assert docs["inputs"] == []
    assert docs["parse_errors"] == []


def test_resync_imports_tags_left_behind(api: Any, e2e_env: Any, connect: Callable[..., dict[str, Any]]) -> None:
    """A module connected without importing has no versions until a resync brings them in."""
    name = _module_name(e2e_env.resource_prefix, "resync")
    connect(name, import_tags=False)
    time.sleep(POLL_SECONDS * 2)
    assert _versions(api, name) == {}, "a module connected with import_tags false imported versions"

    response = api.post(f"{_path(name)}/resync")
    assert response.status_code == 202, f"resync answered {response.status_code}: {response.text[:400]}"
    body = response.json()
    assert body["source"].lower() == f"{NAMESPACE}/{name}/{PROVIDER}".lower()
    assert body["delivery"].startswith("sync-")

    _assert_imported(_wait_for(api, name, _settled))
