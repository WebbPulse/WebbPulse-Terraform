"""Pointing the App's webhook at this API: the manifest for a new App, the sync for an existing one."""

from __future__ import annotations

import json

import pytest

from app.common.composition import settings as settings_module
from app.common.github import loader
from app.common.github.webhooks import WEBHOOK_PATH

API = "https://api.staging.terraform.example.com"
SECRET = "hook-secret-from-the-app-secret"


@pytest.fixture
def api_origin(monkeypatch: pytest.MonkeyPatch) -> str:
    """An API origin for the webhook URL."""
    monkeypatch.setenv("API_BASE_URL", API + "/")
    settings_module.reset_settings_cache()
    return API


@pytest.fixture
def hook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    """A webhook secret in the App's credentials."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    loader.invalidate()
    return SECRET


def test_a_new_app_subscribes_to_the_bridge_events(auth_client, api_origin):
    """With an API origin the manifest carries the webhook URL and the push and pull request events."""
    manifest = auth_client.post("/api/v1/github/app/manifest", json={}).json()["manifest"]
    assert manifest["hook_attributes"] == {"url": f"{API}{WEBHOOK_PATH}", "active": True}
    assert manifest["default_events"] == ["push", "pull_request"]
    assert manifest["default_permissions"]["contents"] == "read"


def test_the_sync_sets_the_url_and_the_secret(auth_client, github, configure_app, api_origin, hook_secret):
    """The App JWT patches the hook config, and the answer never carries the secret."""
    configure_app()
    loader.invalidate()
    response = auth_client.post("/api/v1/github/app/webhook")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "url": f"{API}{WEBHOOK_PATH}",
        "content_type": "json",
        "insecure_ssl": "0",
        "events": ["push", "pull_request"],
    }
    assert SECRET not in response.text
    [request] = [request for request in github.requests if request.url.path == "/app/hook/config"]
    assert request.method == "PATCH"
    assert request.headers["authorization"].startswith("Bearer ey")
    assert json.loads(request.content) == {
        "url": f"{API}{WEBHOOK_PATH}",
        "content_type": "json",
        "secret": SECRET,
        "insecure_ssl": "0",
    }


def test_the_sync_needs_an_app(auth_client, github, api_origin):
    """With no App there is nothing to configure."""
    response = auth_client.post("/api/v1/github/app/webhook")
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_APP_NOT_CONFIGURED"


def test_the_sync_needs_a_secret(auth_client, github, configure_app, api_origin, monkeypatch):
    """An App without a webhook secret is refused before GitHub is called."""
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    configure_app()
    response = auth_client.post("/api/v1/github/app/webhook")
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_WEBHOOK_SECRET_MISSING"
    assert github.requests == []


def test_the_sync_needs_an_api_origin(auth_client, github, configure_app, hook_secret, monkeypatch):
    """Without an API origin there is no URL to point the webhook at."""
    monkeypatch.setenv("API_BASE_URL", "")
    configure_app()
    response = auth_client.post("/api/v1/github/app/webhook")
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_WEBHOOK_URL_MISSING"


def test_a_github_failure_is_a_502(auth_client, github, configure_app, api_origin, hook_secret):
    """GitHub refusing the patch is reported without its message."""
    configure_app()
    github.failure = 403
    response = auth_client.post("/api/v1/github/app/webhook")
    assert response.status_code == 502
    assert response.json()["error_code"] == "GITHUB_UNAVAILABLE"


def test_the_sync_is_admin_only(client):
    """An anonymous caller never reaches GitHub."""
    assert client.post("/api/v1/github/app/webhook").status_code in (401, 403)
