"""The app secret reaches the shared package under the name it actually reads.

A deployed function carries no `IDENTITY_TOTP_MASTER_KEY`: the TOTP master key
lives in the one JSON app secret and `webbpulse.identity` resolves it at runtime
through the `APP_SECRETS_ARN` environment variable directly, never through this
project's settings object. Naming that variable anything else leaves every
`/api/auth/totp/enrol` answering 500 while the whole suite stays green, which is
exactly how it reached staging, so the name is asserted here rather than trusted.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Iterator

import pytest
from webbpulse.identity.crypto import MASTER_KEY_SECRET_ENTRY, resolve_totp_master_key

from app.common.composition.settings import Settings

SECRET_ARN = "arn:aws:secretsmanager:us-west-2:870550636948:secret:webbpulse-terraform-test/app-AbCdEf"

MASTER_KEY = base64.b64encode(b"r" * 32).decode()

TERRAFORM_ENV_VARIABLE = "APP_SECRETS_ARN"
"""The name `terraform/lambda_domains.tf` sets on every domain function."""


class FakeSecretsManager:
    """A Secrets Manager client returning one JSON secret, for the resolver."""

    def __init__(self, payload: dict[str, str]) -> None:
        """Hold the map the fake secret carries."""
        self._payload = payload

    def get_secret_value(self, SecretId: str) -> dict[str, str]:  # noqa: N803
        """Answer with the JSON blob, as the real client does."""
        return {"SecretString": json.dumps(self._payload), "ARN": SecretId}


@pytest.fixture
def secret_environment() -> Iterator[None]:
    """Point `APP_SECRETS_ARN` at a secret for one test, then restore."""
    previous = {name: os.environ.get(name) for name in (TERRAFORM_ENV_VARIABLE, "IDENTITY_TOTP_MASTER_KEY")}
    os.environ[TERRAFORM_ENV_VARIABLE] = SECRET_ARN
    os.environ.pop("IDENTITY_TOTP_MASTER_KEY", None)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_package_resolves_the_totp_key_from_the_terraform_variable(secret_environment: None) -> None:
    """`resolve_totp_master_key` finds the key through the name Terraform sets.

    The resolver reads `APP_SECRETS_ARN` from the environment itself, so this fails
    if Terraform ever renames the variable again.
    """
    client = FakeSecretsManager({MASTER_KEY_SECRET_ENTRY: MASTER_KEY})

    resolved = resolve_totp_master_key(secret_arn=os.environ[TERRAFORM_ENV_VARIABLE], client=client)

    assert resolved == MASTER_KEY


def test_a_missing_master_key_entry_resolves_empty(secret_environment: None) -> None:
    """An app secret without the entry resolves to `""` rather than raising here.

    The named failure belongs to `MfaService`, which reports which variable to set.
    """
    client = FakeSecretsManager({"SECRET_KEY": "unrelated"})

    assert resolve_totp_master_key(secret_arn=os.environ[TERRAFORM_ENV_VARIABLE], client=client) == ""


def test_settings_accept_the_standard_variable(secret_environment: None) -> None:
    """`Settings` reads the standard name and mirrors it onto the base field."""
    settings = Settings()

    assert settings.APP_SECRETS_ARN == SECRET_ARN
    assert settings.app_secret_arn == SECRET_ARN
    assert settings.app_secrets_arn == SECRET_ARN


def test_the_legacy_variable_still_resolves() -> None:
    """`APP_SECRET_ID` keeps working, so a running function survives the rename."""
    previous = {name: os.environ.get(name) for name in (TERRAFORM_ENV_VARIABLE, "APP_SECRET_ID")}
    os.environ.pop(TERRAFORM_ENV_VARIABLE, None)
    os.environ["APP_SECRET_ID"] = SECRET_ARN
    try:
        settings = Settings()
        assert settings.app_secret_arn == SECRET_ARN
        assert settings.app_secrets_arn == SECRET_ARN
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_standard_variable_wins_over_the_legacy_one() -> None:
    """With both set, the standard name is the one the package and this agree on."""
    previous = {name: os.environ.get(name) for name in (TERRAFORM_ENV_VARIABLE, "APP_SECRET_ID")}
    os.environ[TERRAFORM_ENV_VARIABLE] = SECRET_ARN
    os.environ["APP_SECRET_ID"] = "arn:aws:secretsmanager:us-west-2:870550636948:secret:legacy-XxYyZz"
    try:
        assert Settings().app_secret_arn == SECRET_ARN
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
