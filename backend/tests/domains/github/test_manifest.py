"""Creating the App from a manifest: the state, the conversion and the credential write."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import boto3
import pytest

from app.common.composition import settings as settings_module
from app.common.db import repositories
from app.common.github import loader
from app.domains.github import service
from tests.domains.github.conftest import APP_ID, FRONTEND, SLUG


def secret_with(values: dict[str, str]) -> str:
    """Create an `app` secret holding `values` and return its ARN."""
    client = boto3.client("secretsmanager", region_name="us-west-2")
    return client.create_secret(Name="webbpulse-terraform-test-app", SecretString=json.dumps(values))["ARN"]


def secret_values(arn: str) -> dict[str, str]:
    """The secret's current JSON, read back from moto."""
    client = boto3.client("secretsmanager", region_name="us-west-2")
    return json.loads(client.get_secret_value(SecretId=arn)["SecretString"])


@pytest.fixture
def app_secret(monkeypatch):
    """An `app` secret with the keys Terraform manages, wired into the settings."""
    arn = secret_with({"SECRET_KEY": "kept", "variables_master_key": "also-kept"})
    monkeypatch.setenv("APP_SECRETS_ARN", arn)
    settings_module.reset_settings_cache()
    loader.invalidate()
    return arn


def conversion_body(pem: str, *, webhook_secret: str | None = None) -> dict:
    """What `POST /app-manifests/{code}/conversions` returns."""
    body = {
        "id": APP_ID,
        "slug": SLUG,
        "name": SLUG,
        "html_url": f"https://github.com/apps/{SLUG}",
        "owner": {"login": "WebbPulse", "type": "Organization"},
        "client_id": "Iv23liexample",
        "client_secret": "client-secret-value",
        "pem": pem,
    }
    if webhook_secret is not None:
        body["webhook_secret"] = webhook_secret
    return body


def test_the_status_says_there_is_no_app_yet(auth_client):
    """With no credentials the page offers creation and nothing else."""
    body = auth_client.get("/api/v1/github/app").json()
    assert body["configured"] is False
    assert body["can_create"] is True
    assert body["can_install"] is False
    assert body["logo_path"] == "/github-app-logo.png"
    assert body["badge_background"] == "#4d9fff"


def test_the_manifest_carries_both_callbacks(auth_client):
    """GitHub sends the person back to the SPA's create and setup routes."""
    response = auth_client.post("/api/v1/github/app/manifest", json={})
    assert response.status_code == 200
    body = response.json()
    manifest = body["manifest"]
    assert manifest["redirect_url"] == f"{FRONTEND}/settings/github/created"
    assert manifest["setup_url"] == f"{FRONTEND}/settings/github/setup"
    assert manifest["public"] is False
    assert manifest["name"] == "webbpulse-terraform-test"
    assert "hook_attributes" not in manifest
    assert manifest["default_permissions"]["checks"] == "write"
    action = urlparse(body["action_url"])
    assert action.netloc == "github.com"
    assert action.path == "/settings/apps/new"
    assert parse_qs(action.query)["state"] == [body["state"]]


def test_an_organization_owned_app_posts_to_the_organization(auth_client):
    """Naming an organization sends the form to that organization's new App page."""
    body = auth_client.post("/api/v1/github/app/manifest", json={"organization": "WebbPulse"}).json()
    assert urlparse(body["action_url"]).path == "/organizations/WebbPulse/settings/apps/new"


def test_the_manifest_refuses_a_second_app(auth_client, configure_app):
    """One App per environment."""
    configure_app()
    response = auth_client.post("/api/v1/github/app/manifest", json={})
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_APP_ALREADY_CONFIGURED"


def test_the_manifest_needs_a_frontend_origin(auth_client, monkeypatch):
    """Without an origin there is nowhere for GitHub to redirect back to."""
    monkeypatch.setenv("IDENTITY_FRONTEND_BASE_URL", "")
    settings_module.reset_settings_cache()
    response = auth_client.post("/api/v1/github/app/manifest", json={})
    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_FRONTEND_URL_MISSING"


def test_the_conversion_merges_credentials_and_keeps_other_keys(auth_client, github, app_secret, private_key_pem):
    """The five keys land in the secret, Terraform's keys survive, and the App row is stored."""
    github.conversions["code123"] = conversion_body(private_key_pem, webhook_secret="hook-secret")
    state = auth_client.post("/api/v1/github/app/manifest", json={"organization": "WebbPulse"}).json()["state"]

    response = auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": state})

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["slug"] == SLUG
    assert body["app_id"] == str(APP_ID)
    assert body["can_create"] is False
    assert body["can_install"] is True
    assert body["settings_url"] == f"https://github.com/organizations/WebbPulse/settings/apps/{SLUG}"
    assert "pem" not in response.text and "client-secret-value" not in response.text

    stored = secret_values(app_secret)
    assert stored["SECRET_KEY"] == "kept"
    assert stored["variables_master_key"] == "also-kept"
    assert stored["GITHUB_APP_ID"] == str(APP_ID)
    assert stored["GITHUB_PRIVATE_KEY"] == private_key_pem
    assert stored["GITHUB_CLIENT_ID"] == "Iv23liexample"
    assert stored["GITHUB_CLIENT_SECRET"] == "client-secret-value"
    assert stored["GITHUB_WEBHOOK_SECRET"] == "hook-secret"

    row = repositories.github().get(service.APP_KEY)
    assert row is not None
    assert row["slug"] == SLUG
    assert row["owner_type"] == "Organization"
    assert "pem" not in row


def test_the_conversion_without_a_webhook_writes_four_keys(auth_client, github, app_secret, private_key_pem):
    """A manifest with no webhook comes back with no webhook secret, and none is written."""
    github.conversions["code123"] = conversion_body(private_key_pem)
    state = auth_client.post("/api/v1/github/app/manifest", json={}).json()["state"]
    assert (
        auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": state}).status_code == 200
    )
    stored = secret_values(app_secret)
    assert "GITHUB_WEBHOOK_SECRET" not in stored
    assert stored["GITHUB_APP_ID"] == str(APP_ID)
    assert "owner_type" not in (repositories.github().get(service.APP_KEY) or {})


def test_the_conversion_refuses_an_unknown_state(auth_client, github, app_secret, private_key_pem):
    """A forged callback never reaches GitHub."""
    github.conversions["code123"] = conversion_body(private_key_pem)
    response = auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": "x" * 43})
    assert response.status_code == 400
    assert response.json()["error_code"] == "GITHUB_INVALID_STATE"
    assert github.requests == []
    assert "GITHUB_APP_ID" not in secret_values(app_secret)


def test_a_state_works_once(auth_client, github, app_secret, private_key_pem):
    """Replaying the callback fails on the state, before the App check."""
    github.conversions["code123"] = conversion_body(private_key_pem)
    state = auth_client.post("/api/v1/github/app/manifest", json={}).json()["state"]
    assert (
        auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": state}).status_code == 200
    )
    replay = auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": state})
    assert replay.status_code == 400
    assert replay.json()["error_code"] == "GITHUB_INVALID_STATE"


def test_an_install_state_cannot_finish_a_manifest(auth_client, github, configure_app):
    """A state is bound to the step it was issued for."""
    configure_app()
    install_state = auth_client.post("/api/v1/github/install-state").json()["state"]
    response = auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": install_state})
    assert response.status_code == 400
    assert response.json()["error_code"] == "GITHUB_INVALID_STATE"


def test_an_expired_state_is_refused(auth_client, github, app_secret, private_key_pem, monkeypatch):
    """Expiry is checked on use, not left to the TTL sweep."""
    github.conversions["code123"] = conversion_body(private_key_pem)
    state = auth_client.post("/api/v1/github/app/manifest", json={}).json()["state"]
    later = time.time() + service.STATE_TTL_SECONDS[service.MANIFEST] + 1
    monkeypatch.setattr(service.time, "time", lambda: later)
    response = auth_client.post("/api/v1/github/app/conversions", json={"code": "code123", "state": state})
    assert response.status_code == 400
    assert github.requests == []


def test_a_code_github_rejects_is_a_400(auth_client, github, app_secret):
    """An expired or used code comes back from GitHub as a 404, which the page explains."""
    state = auth_client.post("/api/v1/github/app/manifest", json={}).json()["state"]
    response = auth_client.post("/api/v1/github/app/conversions", json={"code": "gone", "state": state})
    assert response.status_code == 400
    assert response.json()["error_code"] == "GITHUB_MANIFEST_CODE_REJECTED"
    assert "GITHUB_APP_ID" not in secret_values(app_secret)
