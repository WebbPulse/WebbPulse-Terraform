"""The S3 state backend's credentials: vended keys scoped to one workspace's state.

The engine's environment carries the workspace run role's keys as `AWS_*` so that
providers act in the workspace's account. The S3 backend would pick the same keys
up, and a run role in another account cannot reach the control plane's state
bucket. The backend override therefore names a profile, which both Terraform and
OpenTofu prefer over environment keys, and this module writes that profile from
the state keys the bundle carries. Those keys are a session of the control
plane's state role narrowed to this workspace's prefix, so nothing the engine
runs can reach another workspace's state with them.
"""

from __future__ import annotations

from pathlib import Path

from app.models import VendedCredentials

STATE_PROFILE = "webbpulse-state"
"""The profile the backend override names."""


def write_profile(directory: Path, credentials: VendedCredentials) -> dict[str, str]:
    """Write the state profile, returning the engine's additions to its environment.

    Both files are owner only and live outside the configuration directory, so
    neither is packed into anything the run uploads. The config file is empty
    and the returned environment points the engine at both, so a stray `~/.aws`
    cannot shadow the profile.
    """
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    config = directory / "config"
    config.write_text("")
    config.chmod(0o600)
    shared = directory / "credentials"
    shared.write_text(
        "\n".join(
            [
                f"[{STATE_PROFILE}]",
                f"aws_access_key_id = {credentials.access_key_id}",
                f"aws_secret_access_key = {credentials.secret_access_key}",
                f"aws_session_token = {credentials.session_token}",
                "",
            ]
        )
    )
    shared.chmod(0o600)
    return {"AWS_CONFIG_FILE": str(config), "AWS_SHARED_CREDENTIALS_FILE": str(shared)}
