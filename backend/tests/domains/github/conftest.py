"""Fixtures for the GitHub domain: an App key, a fake GitHub, and a configured App.

GitHub is replaced at the HTTP layer with `httpx.MockTransport`, so the real client
builds the real requests, signs a real App JWT and pages as it would in production.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.common.composition import settings as settings_module
from app.common.github import loader
from app.domains.github import service

FRONTEND = "https://staging.terraform.example.com"
APP_ID = 424242
SLUG = "webbpulse-terraform-test"


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    """One RSA key for the whole session, since generating it is the slow part."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@dataclass
class FakeGitHub:
    """The routes the domain calls, answered from plain dictionaries."""

    installations: dict[int, dict[str, Any]] = field(default_factory=dict)
    repositories: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    conversions: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    failure: int | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as GitHub would."""
        self.requests.append(request)
        if self.failure is not None:
            return httpx.Response(self.failure, json={"message": "failed"})
        path = request.url.path
        if match := re.fullmatch(r"/app-manifests/([^/]+)/conversions", path):
            body = self.conversions.get(match.group(1))
            return httpx.Response(201, json=body) if body else httpx.Response(404, json={"message": "Not Found"})
        if match := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
            if int(match.group(1)) not in self.installations:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(201, json={"token": f"ghs_{match.group(1)}", "expires_at": "2099-01-01T00:00:00Z"})
        if match := re.fullmatch(r"/app/installations/(\d+)", path):
            found = self.installations.get(int(match.group(1)))
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"message": "Not Found"})
        if path == "/installation/repositories":
            installation_id = int(request.headers["authorization"].removeprefix("Bearer ghs_"))
            everything = self.repositories.get(installation_id, [])
            page = int(request.url.params.get("page", "1"))
            size = int(request.url.params.get("per_page", "30"))
            batch = everything[(page - 1) * size : page * size]
            return httpx.Response(200, json={"total_count": len(everything), "repositories": batch})
        return httpx.Response(404, json={"message": "Not Found"})


def installation_body(installation_id: int, *, login: str = "WebbPulse", suspended: bool = False) -> dict[str, Any]:
    """An installation as `GET /app/installations/{id}` returns it."""
    return {
        "id": installation_id,
        "app_id": APP_ID,
        "account": {"login": login, "type": "Organization", "avatar_url": "https://avatars.example/1"},
        "repository_selection": "selected",
        "html_url": f"https://github.com/organizations/{login}/settings/installations/{installation_id}",
        "permissions": {"metadata": "read"},
        "suspended_at": "2026-09-01T00:00:00Z" if suspended else None,
    }


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    """A fake GitHub every client in the domain talks to."""
    fake = FakeGitHub()
    monkeypatch.setattr(service, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handle)))
    yield fake


@pytest.fixture(autouse=True)
def github_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A frontend origin for the callbacks, no App yet, and an empty settings cache."""
    for key in ("GITHUB_APP_ID", "GITHUB_PRIVATE_KEY", "GITHUB_APP_SLUG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IDENTITY_FRONTEND_BASE_URL", FRONTEND)
    settings_module.reset_settings_cache()
    loader.invalidate()
    yield
    loader.invalidate()
    settings_module.reset_settings_cache()


@pytest.fixture
def configure_app(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> Callable[..., None]:
    """Configure the App through the environment, the way a local run does."""

    def configure(*, slug: str | None = SLUG) -> None:
        """Set the App id, the key and optionally the fallback slug."""
        monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
        monkeypatch.setenv("GITHUB_PRIVATE_KEY", private_key_pem)
        if slug:
            monkeypatch.setenv("GITHUB_APP_SLUG", slug)
        settings_module.reset_settings_cache()
        loader.invalidate()

    return configure


def issued_state(response: httpx.Response | Any) -> str:
    """The state a start endpoint issued."""
    return json.loads(response.text)["state"]
