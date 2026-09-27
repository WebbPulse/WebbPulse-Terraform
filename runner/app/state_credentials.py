"""The S3 state backend's credentials: the runner task's own role, never the run role.

The engine's environment carries the workspace run role as `AWS_*` keys so that
providers act in the workspace's account. The S3 backend would pick the same keys
up, and a run role in another account cannot reach the control plane's state
bucket. The backend override therefore names a profile, which both Terraform and
OpenTofu prefer over environment keys, and this module writes that profile as a
`credential_process` that fetches the task role's credentials from the ECS
container endpoint. The SDK calls the process again when the returned credentials
near their expiry, so an apply that outlives one set of task credentials still
writes its state.

The module also runs as a standalone script, which is what the profile invokes.
It imports nothing from `app`, so it works under `python -I` whatever the
engine's environment says about `PYTHONPATH`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping, Protocol

STATE_PROFILE = "webbpulse-state"
"""The profile the backend override names."""

CONTAINER_KEYS = (
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
)
"""What the ECS agent gives the task to reach its credential endpoint. The engine's
environment is built without them, so they reach the credential process through
the owner only source file instead."""


class StateCredentialsError(RuntimeError):
    """The task role's credentials could not be fetched from the container endpoint."""


class Fetcher(Protocol):
    """The part of botocore's `ContainerMetadataFetcher` the process uses."""

    def retrieve_uri(self, relative_uri: str) -> dict[str, Any]:
        """Fetch from the ECS endpoint by relative path."""
        ...

    def retrieve_full_uri(self, full_url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Fetch from an explicit loopback or ECS endpoint URL."""
        ...


def write_profile(directory: Path, environ: Mapping[str, str], python: str) -> dict[str, str]:
    """Write the credential source and the shared config file, returning the engine's additions.

    Both files are owner only and live outside the configuration directory, so
    neither is packed into anything the run uploads. The returned environment
    points the engine at the config file and at an empty credentials file, so a
    stray `~/.aws` cannot shadow the profile.
    """
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    source = directory / "container.json"
    source.write_text(json.dumps({key: environ[key] for key in CONTAINER_KEYS if environ.get(key)}))
    source.chmod(0o600)
    config = directory / "config"
    config.write_text(
        f"[profile {STATE_PROFILE}]\ncredential_process = {python} -I {Path(__file__).resolve()} {source}\n"
    )
    config.chmod(0o600)
    credentials = directory / "credentials"
    credentials.write_text("")
    credentials.chmod(0o600)
    return {"AWS_CONFIG_FILE": str(config), "AWS_SHARED_CREDENTIALS_FILE": str(credentials)}


def resolve(source: Mapping[str, str], fetcher: Fetcher) -> dict[str, object]:
    """Fetch the task role's credentials and shape them as a version 1 process result."""
    try:
        relative = source.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        full = source.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        if relative:
            response = fetcher.retrieve_uri(relative)
        elif full:
            token = source.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
            token_file = source.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
            if token_file:
                token = Path(token_file).read_text().strip()
            response = fetcher.retrieve_full_uri(full, {"Authorization": token} if token else None)
        else:
            raise StateCredentialsError("the runner task has no container credential endpoint")
    except StateCredentialsError:
        raise
    except Exception as error:
        raise StateCredentialsError(f"container credentials unavailable: {type(error).__name__}") from error
    result: dict[str, object] = {
        "Version": 1,
        "AccessKeyId": response["AccessKeyId"],
        "SecretAccessKey": response["SecretAccessKey"],
        "SessionToken": response["Token"],
    }
    if response.get("Expiration"):
        result["Expiration"] = response["Expiration"]
    return result


def main(argv: list[str]) -> int:
    """Print the task role's credentials for the SDK, or explain on stderr and exit 1."""
    from botocore.utils import ContainerMetadataFetcher

    if len(argv) != 2:
        print("usage: state_credentials.py <container source file>", file=sys.stderr)
        return 1
    try:
        source: dict[str, str] = json.loads(Path(argv[1]).read_text())
        print(json.dumps(resolve(source, ContainerMetadataFetcher())))
    except (OSError, ValueError, KeyError, StateCredentialsError) as error:
        print(f"state credentials: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
