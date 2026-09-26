"""Connecting a workspace to a repository through the environment's GitHub App.

GitHub is replaced at the HTTP layer, so the real client signs a real App JWT, finds
the installation covering the repository and lists what that installation sees.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.common.composition import settings as settings_module
from app.common.db import repositories
from app.common.github import loader
from app.domains.workspaces import vcs_connect
from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"
INSTALLATION_ID = 7001
REPOSITORY = {"id": 555001, "full_name": "WebbPulse/WebbPulse-Terraform", "default_branch": "staging"}
OTHER = {"id": 555002, "full_name": "WebbPulse/Other", "default_branch": "main"}


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    """One RSA key for the module, since generating it is the slow part."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@dataclass
class FakeGitHub:
    """The three routes a connect calls, answered from one installation."""

    repositories: list[dict[str, Any]] = field(default_factory=lambda: [REPOSITORY, OTHER])
    requests: list[httpx.Request] = field(default_factory=list)
    failure: int | None = None
    unlisted: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as GitHub would."""
        self.requests.append(request)
        if self.failure is not None:
            return httpx.Response(self.failure, json={"message": "failed"})
        path = request.url.path
        known = {str(item["full_name"]).lower() for item in self.repositories}
        if match := re.fullmatch(r"/repos/([^/]+/[^/]+)/installation", path):
            if match.group(1).lower() not in known:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"id": INSTALLATION_ID})
        if path == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            listed = [] if self.unlisted else self.repositories
            return httpx.Response(200, json={"total_count": len(listed), "repositories": listed})
        return httpx.Response(404, json={"message": "Not Found"})

    def calls(self) -> list[str]:
        """The paths requested so far."""
        return [request.url.path for request in self.requests]


@pytest.fixture(autouse=True)
def no_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No App until a test configures one, and an empty credentials cache around each test."""
    for key in ("GITHUB_APP_ID", "GITHUB_PRIVATE_KEY"):
        monkeypatch.delenv(key, raising=False)
    loader.invalidate()
    yield
    loader.invalidate()
    settings_module.reset_settings_cache()


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> FakeGitHub:
    """A configured App talking to a fake GitHub."""
    fake = FakeGitHub()
    monkeypatch.setenv("GITHUB_APP_ID", "424242")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", private_key_pem)
    settings_module.reset_settings_cache()
    loader.invalidate()
    monkeypatch.setattr(vcs_connect, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handle)))
    return fake


def _stored(settings, workspace_id: str) -> dict:
    """The raw row, attributes the API does not render included."""
    return repositories.workspaces(settings).get({"workspace_id": workspace_id}) or {}


def test_create_resolves_the_repository(auth_client, github, settings):
    """The id, installation, canonical name and default branch come from the App."""
    payload = WORKSPACE_PAYLOAD | {"vcs_repo": "webbpulse/webbpulse-terraform"}
    response = auth_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["vcs_repo"] == "WebbPulse/WebbPulse-Terraform"
    assert body["vcs_repository_id"] == "555001"
    assert body["vcs_installation_id"] == str(INSTALLATION_ID)
    assert body["tracked_branch"] == "staging"
    assert body["file_triggers_enabled"] is True
    assert _stored(settings, body["workspace_id"])["vcs_repo_key"] == "webbpulse/webbpulse-terraform"


def test_create_keeps_a_requested_branch(auth_client, github):
    """The default branch only fills an absent one."""
    payload = WORKSPACE_PAYLOAD | {"vcs_repo": REPOSITORY["full_name"], "tracked_branch": "release"}
    response = auth_client.post(BASE, json=payload)
    assert response.json()["tracked_branch"] == "release"


def test_a_repository_the_app_cannot_see_is_a_422(auth_client, github):
    """Nothing is written, and the code names the fix."""
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"vcs_repo": "someone/else"})
    assert response.status_code == 422
    assert response.json()["error_code"] == "VCS_REPO_NOT_INSTALLED"
    assert auth_client.get(BASE).json()["items"] == []


def test_an_installation_listing_without_the_repository_is_a_422(auth_client, github):
    """A repository the installation token cannot list is not connected."""
    github.unlisted = True
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"vcs_repo": REPOSITORY["full_name"]})
    assert response.status_code == 422
    assert response.json()["error_code"] == "VCS_REPO_NOT_INSTALLED"


@pytest.mark.parametrize(("failure", "expected"), [(500, 502), (429, 503)])
def test_a_github_failure_is_reported_as_unavailable(auth_client, github, failure, expected):
    """GitHub's own message is never forwarded."""
    github.failure = failure
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"vcs_repo": REPOSITORY["full_name"]})
    assert response.status_code == expected
    assert response.json()["error_code"] == "GITHUB_UNAVAILABLE"
    assert "failed" not in response.text


def test_patch_connects_and_changes_the_repository(auth_client, workspace, github, settings):
    """A new repository replaces the id and the installation, and brings its default branch."""
    url = f"{BASE}/{workspace['workspace_id']}"
    first = auth_client.patch(url, json={"vcs_repo": REPOSITORY["full_name"]}).json()
    assert first["vcs_repository_id"] == "555001"
    assert first["tracked_branch"] == "staging"
    second = auth_client.patch(url, json={"vcs_repo": OTHER["full_name"]}).json()
    assert second["vcs_repository_id"] == "555002"
    assert second["tracked_branch"] == "main"
    assert _stored(settings, workspace["workspace_id"])["vcs_repo_key"] == "webbpulse/other"


def test_resaving_a_connected_repository_calls_github_once(auth_client, workspace, github):
    """The same repository in any case keeps its canonical name and its branch."""
    url = f"{BASE}/{workspace['workspace_id']}"
    auth_client.patch(url, json={"vcs_repo": REPOSITORY["full_name"], "tracked_branch": "feature"})
    before = len(github.requests)
    response = auth_client.patch(url, json={"vcs_repo": "WEBBPULSE/webbpulse-terraform", "speculative_plans": False})
    assert response.status_code == 200, response.text
    assert len(github.requests) == before
    assert response.json()["vcs_repo"] == REPOSITORY["full_name"]
    assert response.json()["tracked_branch"] == "feature"


def test_a_name_only_binding_is_upgraded_without_moving_its_branch(auth_client, workspace, github, settings):
    """A binding made before the App existed gains its id on the next save."""
    repositories.workspaces(settings).update(
        {"workspace_id": workspace["workspace_id"]},
        update_expression="SET vcs_repo = :repo, vcs_repo_key = :key, tracked_branch = :branch",
        expression_values={":repo": REPOSITORY["full_name"], ":key": "webbpulse/webbpulse-terraform", ":branch": "x"},
    )
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"vcs_repo": REPOSITORY["full_name"]})
    assert response.json()["vcs_repository_id"] == "555001"
    assert response.json()["tracked_branch"] == "x"


def test_disconnecting_removes_the_resolved_ids(auth_client, workspace, github, settings):
    """A null clears the name, the key, the id and the installation."""
    url = f"{BASE}/{workspace['workspace_id']}"
    auth_client.patch(url, json={"vcs_repo": REPOSITORY["full_name"]})
    response = auth_client.patch(url, json={"vcs_repo": None, "tracked_branch": None})
    body = response.json()
    assert body["vcs_repo"] is None
    assert body["vcs_repository_id"] is None
    assert body["vcs_installation_id"] is None
    row = _stored(settings, workspace["workspace_id"])
    assert not {"vcs_repo", "vcs_repo_key", "vcs_repository_id", "vcs_installation_id"} & set(row)


def test_without_an_app_the_binding_is_by_name(auth_client, workspace):
    """The first upload records the id instead."""
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"vcs_repo": "WebbPulse/Example"})
    assert response.status_code == 200, response.text
    assert response.json()["vcs_repository_id"] is None
    assert response.json()["vcs_installation_id"] is None
    assert response.json()["tracked_branch"] is None


def test_always_trigger_runs_can_be_set_and_cleared(auth_client, workspace):
    """A null reads back as the default, which filters by path."""
    url = f"{BASE}/{workspace['workspace_id']}"
    assert auth_client.patch(url, json={"file_triggers_enabled": False}).json()["file_triggers_enabled"] is False
    assert auth_client.patch(url, json={"file_triggers_enabled": None}).json()["file_triggers_enabled"] is True


@pytest.mark.parametrize(
    ("given", "stored"),
    [("./stacks/app/", "stacks/app"), ("stacks//app", "stacks/app"), (".", ""), ("  ", ""), ("a/./b", "a/b")],
)
def test_the_working_directory_is_normalized(auth_client, given, stored):
    """A clean relative path, whichever way it was typed."""
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"working_directory": given})
    assert response.status_code == 201, response.text
    assert response.json()["working_directory"] == stored


@pytest.mark.parametrize("given", ["/abs", "../up", "a/../../b", "a\\b", "a\x00b", "x" * 256])
def test_a_working_directory_outside_the_repository_is_a_422(auth_client, workspace, given):
    """The runner would refuse it, so the API refuses it first."""
    assert auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"name": "w", "working_directory": given}).status_code == 422
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"working_directory": given})
    assert response.status_code == 422


@pytest.mark.parametrize("branch", ["main", "release/1.2", "feature-x_y", "v1.0"])
def test_a_valid_branch_is_accepted(auth_client, workspace, branch):
    """Ordinary branch names pass."""
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"tracked_branch": branch})
    assert response.status_code == 200, response.text
    assert response.json()["tracked_branch"] == branch


@pytest.mark.parametrize(
    "branch", ["has space", "a..b", "ends/", "/starts", "-dash", "x.lock", "a~b", "a:b", "@", "a@{b", ".hidden", "a//b"]
)
def test_an_invalid_branch_is_a_422(auth_client, workspace, branch):
    """What git would not accept as a branch is refused."""
    assert auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"name": "b", "tracked_branch": branch}).status_code == 422
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"tracked_branch": branch})
    assert response.status_code == 422


def test_trigger_patterns_are_trimmed_and_deduplicated(auth_client, workspace):
    """Whitespace goes and a repeated pattern is kept once."""
    response = auth_client.patch(
        f"{BASE}/{workspace['workspace_id']}", json={"trigger_patterns": [" stacks/** ", "stacks/**", "modules/*"]}
    )
    assert response.json()["trigger_patterns"] == ["stacks/**", "modules/*"]


@pytest.mark.parametrize("pattern", ["", "   ", "x" * 256])
def test_an_empty_or_oversized_trigger_pattern_is_a_422(auth_client, workspace, pattern):
    """Each pattern has to say something and stay within the limit."""
    assert (
        auth_client.post(BASE, json=WORKSPACE_PAYLOAD | {"name": "p", "trigger_patterns": [pattern]}).status_code == 422
    )
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"trigger_patterns": [pattern]})
    assert response.status_code == 422
