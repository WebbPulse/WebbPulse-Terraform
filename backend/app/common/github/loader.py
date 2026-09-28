"""The GitHub App settings, read from the `app` secret behind a short TTL cache.

`webbpulse.security.app_secrets` caches a secret for the life of the process, which
would leave a warm function blind to App credentials written after its cold start.
This loader passes its own Secrets Manager client on every read, which bypasses that
cache, and holds the answer for `TTL_SECONDS` instead. A missing configuration is
cached for the same time, so a page polling an unconfigured App costs one read a
minute. `invalidate` drops the cache at once, for the function that just wrote it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from webbpulse.integrations.github import GitHubAppSettings, GitHubNotConfigured, load_github_app_settings

TTL_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class _Entry:
    """One cached answer: the settings, or the reason there are none."""

    expires_at: float
    settings: GitHubAppSettings | None
    reason: str


_cache: dict[str, _Entry] = {}
_lock = threading.Lock()


def _secrets_client(region_name: str | None) -> Any:
    """A fresh Secrets Manager client, which is what makes the package skip its cache."""
    import boto3

    return boto3.client("secretsmanager", region_name=region_name)


def github_app_settings(
    secret_arn: str,
    *,
    region_name: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    ttl_seconds: float = TTL_SECONDS,
) -> GitHubAppSettings:
    """The App settings for `secret_arn`, at most `ttl_seconds` old.

    An empty `secret_arn` reads the environment alone, which is the local path. Raises
    `GitHubNotConfigured` when neither carries the App id and private key; the message
    names keys, never values.
    """
    now = clock()
    with _lock:
        entry = _cache.get(secret_arn)
    if entry is None or entry.expires_at <= now:
        entry = _load(secret_arn, region_name, now + ttl_seconds)
        with _lock:
            _cache[secret_arn] = entry
    if entry.settings is None:
        raise GitHubNotConfigured(entry.reason)
    return entry.settings


def _load(secret_arn: str, region_name: str | None, expires_at: float) -> _Entry:
    """Read the secret once and wrap the outcome for the cache."""
    client = _secrets_client(region_name) if secret_arn else None
    try:
        settings = load_github_app_settings(secret_arn, client=client)
    except GitHubNotConfigured as exc:
        return _Entry(expires_at, None, str(exc))
    return _Entry(expires_at, settings, "")


def invalidate(secret_arn: str | None = None) -> None:
    """Forget the cached answer for `secret_arn`, or for every secret."""
    with _lock:
        if secret_arn is None:
            _cache.clear()
        else:
            _cache.pop(secret_arn, None)
