"""Unpacking the config tarball and writing the backend and variable files."""

from __future__ import annotations

import json
import re
import tarfile
from collections.abc import Callable, Iterable
from pathlib import Path

from app.credential_files import STATE_PROFILE
from app.models import BackendConfig, Bundle

BACKEND_FILENAME = "zz_webbpulse_backend_override.tf"
TFVARS_FILENAME = "zz_webbpulse.auto.tfvars.json"
HCL_TFVARS_FILENAME = "zz_webbpulse.auto.tfvars"

DATA_DIRECTORY_EXCLUDED = ("providers", "terraform.tfstate")
"""Data directory entries the workdir archive leaves out: the provider binaries, which
the apply's `init` installs again from the lock file, and the backend record it rewrites."""


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
"""What an input variable may be named, matched before a name is written into HCL
unquoted. HCL writes use the same restriction in the API."""


class ConfigError(RuntimeError):
    """The config tarball is absent, unreadable or tries to escape the directory."""


def unpack_config(archive: Path, directory: Path, *, allow_links: bool = False) -> Path:
    """Extract the config tarball into the working directory, rejecting escaping members.

    A config upload never carries links. A restored plan working directory may, as
    installed modules can, so `allow_links` admits them and the `data` filter still
    refuses any whose target leaves the directory.
    """
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as handle:
            for member in handle.getmembers():
                target = (directory / member.name).resolve()
                if not str(target).startswith(str(directory.resolve())):
                    raise ConfigError(f"config archive member escapes the working directory: {member.name}")
                if (member.issym() or member.islnk()) and not allow_links:
                    raise ConfigError(f"config archive carries a link member: {member.name}")
            handle.extractall(directory, filter="data")
    except tarfile.TarError as error:
        raise ConfigError("config archive could not be read") from error
    return directory


def write_backend_override(directory: Path, backend: BackendConfig) -> Path:
    """Write the S3 backend override with native locking on.

    The backend names the state profile, so state is always read and written
    with the vended state keys while the providers get the run role from the
    environment. A run role in another account never reaches the state bucket.
    Only the profile name is written, so neither the plan file nor `.terraform`
    records a credential. CLI workspace states go under the workspace's own
    prefix too, the only one its state keys can list.
    """
    body = "\n".join(
        [
            "terraform {",
            '  backend "s3" {',
            f'    bucket       = "{backend.bucket}"',
            f'    key          = "{backend.key}"',
            f'    workspace_key_prefix = "{backend.key.rsplit("/", 1)[0]}/env"',
            f'    region       = "{backend.region}"',
            f'    kms_key_id   = "{backend.kms_key_id}"',
            "    encrypt      = true",
            "    use_lockfile = true",
            f'    profile      = "{STATE_PROFILE}"',
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


def prepare(directory: Path, bundle: Bundle, archive: Path, *, allow_links: bool = False) -> Path:
    """Unpack the config, resolve the working directory and lay down the files.

    The backend override and the tfvars files go in the working directory rather
    than the tarball root, because that is the directory the engine is run from
    and none of them is loaded from anywhere else.

    Literal and HCL valued variables go to two different files on purpose: the
    JSON one cannot reinterpret a literal, and the native one is the only place
    an expression is parsed.
    """
    unpack_config(archive, directory, allow_links=allow_links)
    target = resolve_working_directory(directory, bundle.working_directory)
    write_backend_override(target, bundle.backend)
    write_tfvars(target, bundle.terraform_variables)
    write_hcl_tfvars(target, bundle.hcl_variables)
    return target


def _workdir_member(excluded: frozenset[str]) -> Callable[[tarfile.TarInfo], tarfile.TarInfo | None]:
    """A tar filter keeping files, directories and links outside `excluded`."""

    def keep(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = member.name.removeprefix("./")
        if any(name == path or name.startswith(f"{path}/") for path in excluded):
            return None
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            return None
        member.uid = member.gid = 0
        member.uname = member.gname = ""
        return member

    return keep


def pack_workdir(
    directory: Path, working_directory: Path, destination: Path, data_directory: str, extra: Iterable[str] = ()
) -> Path:
    """Archive the planned configuration for the apply phase to restore in its place.

    HCP Terraform applies in the plan's own working directory, so installed modules
    and files the plan generated, such as an `archive_file` zip a lambda reads at
    apply, are there. The archive holds the whole configuration root with the data
    directory's modules. It leaves out the provider binaries, the backend record, the
    files the runner writes from the bundle (the backend override and both tfvars
    files, which carry variable values) and `extra`, such as the plan file the apply
    downloads on its own. Links are archived as links, never followed.
    """
    relative = working_directory.resolve().relative_to(directory.resolve())

    def under(name: str) -> str:
        return (relative / name).as_posix().removeprefix("./")

    excluded = frozenset(
        [under(name) for name in (BACKEND_FILENAME, TFVARS_FILENAME, HCL_TFVARS_FILENAME, *extra)]
        + [under(f"{data_directory}/{name}") for name in DATA_DIRECTORY_EXCLUDED]
    )
    with tarfile.open(destination, "w:gz") as archive:
        for child in sorted(directory.iterdir()):
            archive.add(child, arcname=child.name, recursive=True, filter=_workdir_member(excluded))
    return destination
