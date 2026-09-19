"""Unpacking the config tarball and writing the backend and variable files."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from app.models import BackendConfig, Bundle

BACKEND_FILENAME = "zz_webbpulse_backend_override.tf"
TFVARS_FILENAME = "zz_webbpulse.auto.tfvars.json"


class ConfigError(RuntimeError):
    """The config tarball is absent, unreadable or tries to escape the directory."""


def unpack_config(archive: Path, directory: Path) -> Path:
    """Extract the config tarball into the working directory, rejecting escaping members."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as handle:
            for member in handle.getmembers():
                target = (directory / member.name).resolve()
                if not str(target).startswith(str(directory.resolve())):
                    raise ConfigError(f"config archive member escapes the working directory: {member.name}")
                if member.issym() or member.islnk():
                    raise ConfigError(f"config archive carries a link member: {member.name}")
            handle.extractall(directory, filter="data")
    except tarfile.TarError as error:
        raise ConfigError("config archive could not be read") from error
    return directory


def write_backend_override(directory: Path, backend: BackendConfig) -> Path:
    """Write the S3 backend override with native locking on."""
    body = "\n".join(
        [
            "terraform {",
            '  backend "s3" {',
            f'    bucket       = "{backend.bucket}"',
            f'    key          = "{backend.key}"',
            f'    region       = "{backend.region}"',
            f'    kms_key_id   = "{backend.kms_key_id}"',
            "    encrypt      = true",
            "    use_lockfile = true",
            "  }",
            "}",
            "",
        ]
    )
    path = directory / BACKEND_FILENAME
    path.write_text(body)
    return path


def write_tfvars(directory: Path, variables: dict[str, object]) -> Path | None:
    """Write the terraform variables as an auto loaded tfvars file, restricted to the owner."""
    if not variables:
        return None
    path = directory / TFVARS_FILENAME
    path.write_text(json.dumps(variables, sort_keys=True))
    path.chmod(0o600)
    return path


def resolve_working_directory(directory: Path, working_directory: str) -> Path:
    """Resolve the bundle's working directory under the unpacked configuration.

    The value comes from the workspace record, so it is operator supplied rather
    than trusted: an absolute path or one climbing out with `..` would point the
    engine at the task filesystem instead of the configuration, so both are
    refused rather than normalised away.
    """
    candidate = working_directory.strip()
    if not candidate:
        return directory
    if Path(candidate).is_absolute():
        raise ConfigError(f"working directory escapes the configuration: {working_directory}")
    relative = candidate.strip("/")
    if not relative:
        return directory
    root = directory.resolve()
    target = (root / relative).resolve()
    if target != root and root not in target.parents:
        raise ConfigError(f"working directory escapes the configuration: {working_directory}")
    if not target.is_dir():
        raise ConfigError(f"working directory is not in the configuration: {working_directory}")
    return target


def prepare(directory: Path, bundle: Bundle, archive: Path) -> Path:
    """Unpack the config, resolve the working directory and lay down the files.

    The backend override and the tfvars file go in the working directory rather
    than the tarball root, because that is the directory the engine is run from
    and neither file is loaded from anywhere else.
    """
    unpack_config(archive, directory)
    target = resolve_working_directory(directory, bundle.working_directory)
    write_backend_override(target, bundle.backend)
    write_tfvars(target, bundle.terraform_variables)
    return target
