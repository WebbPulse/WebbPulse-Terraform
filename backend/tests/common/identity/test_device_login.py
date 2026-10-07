"""Device login tokens on the product routes, and the identity glue that serves them.

A `wp-tf login` token reaches a route as JWT claims carrying `grant: device`. It is
honoured only while device login is on and its grant is live, so a revoked or
expired login stops working within the liveness cache rather than at token expiry.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from webbpulse.http import REQUEST_CONTEXT_HEADER
from webbpulse.identity import ConsentTheme, dynamo_device_grant_stores
from webbpulse.identity.device_grant_storage import DeviceGrantRecord

from app.common.composition import settings as settings_module
from app.common.core import auth
from app.common.core.auth import ALL_SCOPES
from app.common.db.identity_tables import identity_table_prefix
from app.common.identity.package_glue import build_identity_settings, consent_theme
from tests.conftest import person_headers, seed_user

USER_ID = "user-device"
GRANT_ID = "grant-1"


@pytest.fixture(autouse=True)
def _fresh_liveness() -> Iterator[None]:
    """Drop cached liveness checks, which hold a client bound to the previous moto context."""
    auth.reset_device_grant_liveness()
    yield
    auth.reset_device_grant_liveness()


@pytest.fixture
def device_enabled(monkeypatch: pytest.MonkeyPatch, identity_tables: str) -> Iterator[None]:
    """Turn device login on for one test."""
    del identity_tables
    monkeypatch.setenv("IDENTITY_DEVICE_GRANT_ENABLED", "true")
    settings_module.reset_settings_cache()
    yield
    settings_module.reset_settings_cache()


def put_grant(*, revoked: bool = False, expires_in: int = 3600) -> None:
    """Store the test user's device grant."""
    settings = settings_module.get_settings()
    stores = dynamo_device_grant_stores(
        identity_table_prefix(settings),
        region_name=settings.AWS_REGION_NAME,
        endpoint_url=settings.dynamodb_endpoint_url,
    )
    stores.grants.put(
        DeviceGrantRecord(
            grant_id=GRANT_ID,
            user_id=USER_ID,
            client_id="wp-tf",
            scopes=tuple(ALL_SCOPES),
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=int(time.time()) + expires_in,
            refresh_hash="hash",
            revoked=revoked,
        )
    )


def device_headers(*, sid: str = GRANT_ID, auth_age: int = 0) -> dict[str, str]:
    """The request context a device login token produces behind the gateway authorizer."""
    claims = {
        "sub": USER_ID,
        "scope": " ".join(ALL_SCOPES),
        "roles": "[]",
        "grant": "device",
        "sid": sid,
        "auth_time": str(int(time.time()) - auth_age),
    }
    return {REQUEST_CONTEXT_HEADER: json.dumps({"authorizer": {"jwt": {"claims": claims}}})}


def list_workspaces(app) -> int:
    """The status a device token gets listing workspaces."""
    with TestClient(app) as client:
        return client.get("/api/v1/workspaces", headers=device_headers()).status_code


def test_a_device_token_is_refused_while_device_login_is_off(app) -> None:
    seed_user(USER_ID)
    assert auth.device_grant_liveness() is None
    assert list_workspaces(app) == 401


def test_a_live_device_grant_reaches_the_route(device_enabled: None, app) -> None:
    seed_user(USER_ID)
    put_grant()
    assert list_workspaces(app) == 200


def test_a_revoked_device_grant_is_refused(device_enabled: None, app) -> None:
    seed_user(USER_ID)
    put_grant(revoked=True)
    assert list_workspaces(app) == 401


def test_an_expired_device_grant_is_refused(device_enabled: None, app) -> None:
    seed_user(USER_ID)
    put_grant(expires_in=-60)
    assert list_workspaces(app) == 401


def test_a_device_token_naming_no_stored_grant_is_refused(device_enabled: None, app) -> None:
    seed_user(USER_ID)
    with TestClient(app) as client:
        assert client.get("/api/v1/workspaces", headers=device_headers(sid="missing")).status_code == 401


def test_the_liveness_check_is_shared_across_requests(device_enabled: None) -> None:
    assert auth.device_grant_liveness() is auth.device_grant_liveness()


def test_the_consent_theme_defaults_to_dark() -> None:
    theme = consent_theme()
    assert isinstance(theme, ConsentTheme)
    assert theme.color_scheme == "dark"


DEVICE_ENVIRONMENT = {
    "IDENTITY_DEVICE_GRANT_ENABLED": "true",
    "IDENTITY_FRONTEND_BASE_URL": "https://terraform.example.test",
}
"""What Terraform sets alongside the identity variables when device login is on. The
client, scopes and sign-in page are product facts the identity builder supplies."""


def test_device_login_facts_come_from_code_not_the_environment(
    monkeypatch: pytest.MonkeyPatch, identity_environment: None
) -> None:
    del identity_environment
    for key, value in DEVICE_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    settings_module.reset_settings_cache()
    identity = build_identity_settings(settings_module.get_settings())
    settings_module.reset_settings_cache()
    assert identity.device_grant_enabled
    assert identity.device_clients == {"wp-tf": "wp-tf CLI"}
    assert identity.device_scopes_supported == list(ALL_SCOPES)
    assert identity.device_explicit_scopes == ["state:download", "state:write", "admin"]
    assert identity.device_login_url == "https://terraform.example.test/sign-in"
    assert identity.device_audience == ""
    assert identity.webauthn_origins == ["https://terraform.example.test"]


def test_browser_sessions_slide_for_12_hours_up_to_7_days(identity_environment: None) -> None:
    """The refresh window rolls 12 hours from each refresh and stops 7 days after the sign-in."""
    del identity_environment
    settings_module.reset_settings_cache()
    identity = build_identity_settings(settings_module.get_settings())
    settings_module.reset_settings_cache()
    assert identity.refresh_token_ttl == timedelta(hours=12)
    assert identity.refresh_absolute_ttl == timedelta(days=7)
    assert identity.cookie_kwargs()["max_age"] == 12 * 3600


def test_an_explicit_webauthn_origin_wins(monkeypatch: pytest.MonkeyPatch, identity_environment: None) -> None:
    del identity_environment
    monkeypatch.setenv("IDENTITY_FRONTEND_BASE_URL", "https://terraform.example.test")
    monkeypatch.setenv("IDENTITY_WEBAUTHN_ORIGINS", '["http://127.0.0.1:4173"]')
    settings_module.reset_settings_cache()
    identity = build_identity_settings(settings_module.get_settings())
    settings_module.reset_settings_cache()
    assert identity.webauthn_origins == ["http://127.0.0.1:4173"]
    assert not identity.device_grant_enabled


def test_the_device_routes_mount_and_start_a_login(monkeypatch: pytest.MonkeyPatch, identity_environment: None) -> None:
    del identity_environment
    for key, value in DEVICE_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    settings_module.reset_settings_cache()
    from app.common.composition.app import build_app

    with TestClient(build_app(settings_module.get_settings())) as client:
        started = client.post("/api/auth/device/code", data={"client_id": "wp-tf"})
        refused = client.post("/api/auth/device/code", data={"client_id": "other"})
    settings_module.reset_settings_cache()
    assert started.status_code == 200
    body = started.json()
    assert body["user_code"]
    assert body["verification_uri"].endswith("/api/auth/device")
    assert refused.status_code >= 400


WP_TF_STANDARD_SCOPES = (
    "workspaces:read",
    "workspaces:write",
    "variables:read",
    "variables:write",
    "configs:read",
    "configs:write",
    "runs:read",
    "runs:write",
    "runs:apply",
    "registry:read",
    "registry:write",
)
"""`STANDARD_SCOPES` in `webbpulse.tf.cli`, which `wp-tf login --add-scope` builds on."""


def test_a_default_device_login_gets_the_wp_tf_standard_scopes(
    monkeypatch: pytest.MonkeyPatch, identity_environment: None
) -> None:
    del identity_environment
    for key, value in DEVICE_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    settings_module.reset_settings_cache()
    identity = build_identity_settings(settings_module.get_settings())
    settings_module.reset_settings_cache()
    explicit = set(identity.device_explicit_scopes)
    default = [scope for scope in identity.device_scopes_supported if scope not in explicit]
    assert sorted(default) == sorted(WP_TF_STANDARD_SCOPES)


def test_a_device_session_applies_on_an_old_login(device_enabled: None, app, awaiting_confirmation) -> None:
    """The device approval was the step-up, so an hour later the session still confirms."""
    seed_user(USER_ID)
    put_grant()
    run_id = awaiting_confirmation["run_id"]
    with TestClient(app) as client:
        response = client.post(f"/api/v1/runs/{run_id}/confirm", headers=device_headers(auth_age=3600))
    assert response.status_code == 200, response.text
    assert response.json()["decision"]["actor"]["id"] == USER_ID


def test_a_browser_session_on_an_old_login_applies(app, awaiting_confirmation) -> None:
    """A browser session confirms on `runs:apply` alone, like a device session, with no step-up."""
    seed_user(USER_ID)
    run_id = awaiting_confirmation["run_id"]
    with TestClient(app, headers=person_headers(user_id=USER_ID, auth_age=3600)) as client:
        response = client.post(f"/api/v1/runs/{run_id}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["decision"]["actor"]["id"] == USER_ID


def test_a_revoked_device_session_cannot_apply(device_enabled: None, app, awaiting_confirmation) -> None:
    seed_user(USER_ID)
    put_grant(revoked=True)
    run_id = awaiting_confirmation["run_id"]
    with TestClient(app) as client:
        response = client.post(f"/api/v1/runs/{run_id}/confirm", headers=device_headers())
    assert response.status_code == 401
