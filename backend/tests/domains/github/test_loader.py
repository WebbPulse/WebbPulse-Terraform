"""The App settings loader: a short TTL over the `app` secret, and nothing longer."""

from __future__ import annotations

import json

import boto3
import pytest
from webbpulse.integrations.github import GitHubNotConfigured

from app.common.github import loader
from tests.domains.github.conftest import APP_ID


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current time."""
        return self.now


@pytest.fixture
def secret_arn() -> str:
    """An `app` secret with no GitHub keys yet."""
    client = boto3.client("secretsmanager", region_name="us-west-2")
    return client.create_secret(Name="loader-test-app", SecretString=json.dumps({"SECRET_KEY": "x"}))["ARN"]


def write(arn: str, values: dict[str, str]) -> None:
    """Replace the secret's JSON."""
    boto3.client("secretsmanager", region_name="us-west-2").put_secret_value(
        SecretId=arn, SecretString=json.dumps(values)
    )


def test_a_missing_app_is_cached_then_seen_after_the_ttl(secret_arn, private_key_pem):
    """Credentials written after the first read are picked up once the TTL passes."""
    clock = Clock()
    with pytest.raises(GitHubNotConfigured):
        loader.github_app_settings(secret_arn, clock=clock)

    write(secret_arn, {"GITHUB_APP_ID": str(APP_ID), "GITHUB_PRIVATE_KEY": private_key_pem})
    clock.now = loader.TTL_SECONDS - 1
    with pytest.raises(GitHubNotConfigured):
        loader.github_app_settings(secret_arn, clock=clock)

    clock.now = loader.TTL_SECONDS + 1
    assert loader.github_app_settings(secret_arn, clock=clock).app_id == str(APP_ID)


def test_a_configured_app_is_served_from_the_cache(secret_arn, private_key_pem):
    """Inside the TTL a removed key is not noticed, which is the point of the cache."""
    clock = Clock()
    write(secret_arn, {"GITHUB_APP_ID": str(APP_ID), "GITHUB_PRIVATE_KEY": private_key_pem})
    assert loader.github_app_settings(secret_arn, clock=clock).app_id == str(APP_ID)
    write(secret_arn, {})
    clock.now = 1
    assert loader.github_app_settings(secret_arn, clock=clock).app_id == str(APP_ID)
    clock.now = loader.TTL_SECONDS + 2
    with pytest.raises(GitHubNotConfigured):
        loader.github_app_settings(secret_arn, clock=clock)


def test_invalidate_rereads_at_once(secret_arn, private_key_pem):
    """The function that wrote the credentials sees them on its next call."""
    clock = Clock()
    with pytest.raises(GitHubNotConfigured):
        loader.github_app_settings(secret_arn, clock=clock)
    write(secret_arn, {"GITHUB_APP_ID": str(APP_ID), "GITHUB_PRIVATE_KEY": private_key_pem})
    loader.invalidate(secret_arn)
    assert loader.github_app_settings(secret_arn, clock=clock).app_id == str(APP_ID)


def test_the_error_names_keys_not_values(secret_arn):
    """An invalid key is reported by name only."""
    write(secret_arn, {"GITHUB_APP_ID": "1", "GITHUB_PRIVATE_KEY": "not-a-pem-value"})
    with pytest.raises(GitHubNotConfigured) as caught:
        loader.github_app_settings(secret_arn, clock=Clock())
    assert "not-a-pem-value" not in str(caught.value)
    assert "GITHUB_PRIVATE_KEY" in str(caught.value)
