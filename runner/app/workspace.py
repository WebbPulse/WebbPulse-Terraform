"""Unpacking the config tarball and writing the backend and variable files."""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path

from app.models import BackendConfig, Bundle

BACKEND_FILENAME = "zz_webbpulse_backend_override.tf"
TFVARS_FILENAME = "zz_webbpulse.auto.tfvars.json"
HCL_TFVARS_FILENAME = "zz_webbpulse.auto.tfvars"


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
"""What an input variable may be named, matched before a name is written into HCL
unquoted. HCL writes use the same restriction in the API."""


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
    """Write the literal terraform variables as a JSON tfvars file, owner only.

    JSON is what makes a literal literal. A value in a `.tfvars.json` file is
    taken as the JSON value it is, so a string stays a string however many quotes,
    braces or `${` sequences it contains, and nothing in it can be reinterpreted
    as an expression. HCL valued variables are written by `write_hcl_tfvars`
    instead, because that is the opposite requirement.
    """
    if not variables:
        return None
    path = directory / TFVARS_FILENAME
    path.write_text(json.dumps(variables, sort_keys=True))
    path.chmod(0o600)
    return path


def write_hcl_tfvars(directory: Path, variables: dict[str, str]) -> Path | None:
    """Write the HCL valued variables as a native tfvars file, owner only.

    A native `.tfvars` file is HCL, so the right hand side of each assignment is
    an expression the engine parses. The expression is emitted exactly as it was
    stored, unquoted and unescaped: quoting it would turn `["a", "b"]` into the
    eight character string `["a", "b"]` and silently give a `list(string)` input
    variable a value of the wrong type, which is the corruption this file exists
    to avoid.

    Each expression is grouped on separate lines so leading and trailing comments
    and multiline expressions remain inside their own assignment.

    Terraform loads `.auto.tfvars` and `.auto.tfvars.json` files together in
    lexical order, and a key is never in both files, so the two never contend.

    Raises:
        ConfigError: A key is not a usable variable name. The name is written into
            an HCL file unquoted, so anything but an identifier could change the
            file's meaning rather than name a variable.
    """
    if not variables:
        return None
    lines: list[str] = []
    for key in sorted(variables):
        if not _VARIABLE_NAME.fullmatch(key):
            raise ConfigError(f"variable name is not a valid HCL identifier: {key}")
        lines.append(f"{key} = (\n{variables[key]}\n)")
    path = directory / HCL_TFVARS_FILENAME
    path.write_text("\n".join(lines) + "\n")
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

    The backend override and the tfvars files go in the working directory rather
    than the tarball root, because that is the directory the engine is run from
    and none of them is loaded from anywhere else.

    Literal and HCL valued variables go to two different files on purpose: the
    JSON one cannot reinterpret a literal, and the native one is the only place
    an expression is parsed.
    """
    unpack_config(archive, directory)
    target = resolve_working_directory(directory, bundle.working_directory)
    write_backend_override(target, bundle.backend)
    write_tfvars(target, bundle.terraform_variables)
    write_hcl_tfvars(target, bundle.hcl_variables)
    return target
