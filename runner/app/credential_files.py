"""The engine's AWS credentials, served from files the runner rewrites as sessions are refreshed.

The control plane vends the workspace run role session for the providers and a
state role session narrowed to the workspace prefix for the S3 backend. Both are
chained role sessions, so neither outlives an hour, while an apply may run for
several. Environment keys and static profile keys are read once when the engine
starts, so they cannot be rotated. Each session is instead a `credential_process`
profile that prints a JSON file with an `Expiration`: the AWS SDK inside the
engine and its providers caches the keys until then and runs the process again
afterwards, by which time the runner has rewritten the file with a fresh session.

The advertised expiry sits `EXPIRY_MARGIN_SECONDS` before the real one, so the
SDK rereads the file while the keys it holds still work. The backend override
names the state profile explicitly; `AWS_PROFILE` points everything else at the
run profile.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.isolation import share
from app.models import VendedCredentials

RUN_PROFILE = "webbpulse-run"
"""The profile the providers read through `AWS_PROFILE`."""

STATE_PROFILE = "webbpulse-state"
"""The profile the backend override names."""

EXPIRY_MARGIN_SECONDS = 300
"""How long before a session's real expiry the SDK is told it expires."""


def parse_expiration(value: str) -> datetime | None:
    """An ISO 8601 expiry as an aware UTC datetime, or None when absent or unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def process_document(credentials: VendedCredentials, margin_seconds: int = EXPIRY_MARGIN_SECONDS) -> dict[str, object]:
    """The `credential_process` output for one session, expiring `margin_seconds` early."""
    document: dict[str, object] = {
        "Version": 1,
        "AccessKeyId": credentials.access_key_id,
        "SecretAccessKey": credentials.secret_access_key,
        "SessionToken": credentials.session_token,
    }
    expiration = parse_expiration(credentials.expiration)
    if expiration is not None:
        advertised = expiration - timedelta(seconds=margin_seconds)
        document["Expiration"] = advertised.strftime("%Y-%m-%dT%H:%M:%SZ")
    return document


def private_directory(directory: Path, group: int | None = None) -> None:
    """Create `directory` for the runner alone, or readable by `group` as well when one is given."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    share(directory, group)


def write_private(path: Path, body: str, group: int | None = None) -> None:
    """Replace `path` atomically with an owner only file, so a reader never sees half a document.

    With `group` the file is readable by that group too, which is how the engine
    user reads what the runner writes without being able to rewrite it.
    """
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(body)
        os.chmod(temporary, 0o600)
        share(Path(temporary), group)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class CredentialFiles:
    """The run and state profiles and the session files behind them, in one owner only directory.

    Everything lives outside the configuration directory, so nothing here is
    packed into what the run uploads.
    """

    def __init__(self, directory: Path, margin_seconds: int = EXPIRY_MARGIN_SECONDS, group: int | None = None) -> None:
        self.directory = directory
        self._margin_seconds = margin_seconds
        self._group = group
        self.expires_at: datetime | None = None

    @property
    def config_path(self) -> Path:
        """The SDK config file naming both profiles."""
        return self.directory / "config"

    @property
    def credentials_path(self) -> Path:
        """An empty shared credentials file, so a stray `~/.aws/credentials` cannot shadow a profile."""
        return self.directory / "credentials"

    def session_path(self, profile: str) -> Path:
        """The JSON file one profile's process prints."""
        return self.directory / f"{profile}.json"

    def write(self, provider: VendedCredentials, state: VendedCredentials) -> None:
        """Write or rotate both sessions, creating the profiles on first use.

        `expires_at` becomes the earlier of the two real expiries, which is when
        the next refresh is due.
        """
        private_directory(self.directory, self._group)
        for profile, credentials in ((RUN_PROFILE, provider), (STATE_PROFILE, state)):
            write_private(
                self.session_path(profile),
                json.dumps(process_document(credentials, self._margin_seconds)),
                self._group,
            )
        if not self.config_path.exists():
            write_private(self.config_path, self._config(), self._group)
            write_private(self.credentials_path, "", self._group)
        expiries = [
            expiry for expiry in (parse_expiration(provider.expiration), parse_expiration(state.expiration)) if expiry
        ]
        self.expires_at = min(expiries) if expiries else None

    def _config(self) -> str:
        lines: list[str] = []
        for profile in (RUN_PROFILE, STATE_PROFILE):
            command = f"cat {shlex.quote(str(self.session_path(profile)))}"
            lines.extend([f"[profile {profile}]", f"credential_process = {command}", ""])
        return "\n".join(lines)

    def environment(self) -> dict[str, str]:
        """The engine's additions to its environment, pointing the SDK at these files alone."""
        return {
            "AWS_CONFIG_FILE": str(self.config_path),
            "AWS_SHARED_CREDENTIALS_FILE": str(self.credentials_path),
            "AWS_PROFILE": RUN_PROFILE,
            "AWS_SDK_LOAD_CONFIG": "1",
        }
