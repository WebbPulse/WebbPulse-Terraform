"""The environment an identity test needs, and the app built under it.

The identity routes are declared from `IdentitySettings`, which reads the
environment at build time, so these variables have to be in place before the app is
built rather than injected afterwards.

The signer is the package's local one. It derives its key from a seed in process, so
these tests verify a real RS256 JWKS with no KMS call and no credential. The package
refuses that signer in production, so nothing here can leak into a deployed stack.
"""

from __future__ import annotations

import base64
import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.common.composition import settings as settings_module

ISSUER = "https://api.example.test/api/auth"

AUDIENCE = "webbpulse-terraform-test-api"

IDENTITY_ENVIRONMENT = {
    "IDENTITY_ISSUER": ISSUER,
    "IDENTITY_AUDIENCE": AUDIENCE,
    "IDENTITY_ENVIRONMENT": "local",
    "IDENTITY_SIGNER": "local",
    "IDENTITY_SIGNING_KEY_ARNS": '["local"]',
    "IDENTITY_LOCAL_SIGNER_SEED": "seed-for-the-in-process-signer",
    "IDENTITY_RP_ID": "example.test",
    "IDENTITY_RP_NAME": "WebbPulse Terraform",
    "IDENTITY_REGISTRATION_ENABLED": "false",
    "IDENTITY_TOTP_CIPHER": "secret",
    "IDENTITY_TOTP_MASTER_KEY": base64.b64encode(b"m" * 32).decode(),
}
"""What Terraform sets on the workspaces function, with the signer moved to local.

`IDENTITY_SIGNING_KEY_ARNS` is present because the settings refuse an empty list
even when the local signer never reads it, and `IDENTITY_TOTP_MASTER_KEY` because
`IDENTITY_TOTP_CIPHER` is `secret` here exactly as it is in staging."""


@pytest.fixture
def identity_environment(identity_tables: str) -> Iterator[None]:
    """Set the `IDENTITY_*` variables for one test, and restore them afterwards.

    The settings cache is cleared on both sides, because a cached `Settings` built
    before these variables landed carries an empty `IDENTITY_ISSUER` and the router
    would then not mount at all.
    """
    del identity_tables
    previous = {key: os.environ.get(key) for key in IDENTITY_ENVIRONMENT}
    os.environ.update(IDENTITY_ENVIRONMENT)
    settings_module.reset_settings_cache()
    yield
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    settings_module.reset_settings_cache()


@pytest.fixture
def identity_app(identity_environment: None):
    """The whole surface built with identity configured, so `/api/auth` is mounted."""
    del identity_environment
    from app.common.composition.app import build_app

    return build_app(settings_module.get_settings())


@pytest.fixture
def identity_client(identity_app) -> Iterator[TestClient]:
    """An unauthenticated client, which is what the anonymous identity routes take."""
    with TestClient(identity_app) as client:
        yield client
