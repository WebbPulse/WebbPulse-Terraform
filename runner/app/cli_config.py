"""The engine's CLI config, which answers another registry host's module lookups from this plane.

Until cutover, module sources name `app.terraform.io`. A `host` block forces that
host's service discovery to this plane's `modules.v1` URL, so `init` never asks
HCP Terraform anything, and the engine sends the run's own registry credential,
set as that host's `TF_TOKEN_<host>`, to this plane instead. The file holds no
secret. Terraform and OpenTofu both read it through `TF_CLI_CONFIG_FILE`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from app.credential_files import private_directory, write_private

CONFIG_VARIABLE = "TF_CLI_CONFIG_FILE"
"""The variable both engines read their CLI config path from."""

FILE_NAME = "terraform.tfrc"
"""The config's name inside the directory the runner gives it."""

HOST_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
"""A lowercase dotted hostname, with no port, path or quoting."""


class CliConfigError(ValueError):
    """A host or modules URL the config cannot safely name."""


def render(module_hosts: Mapping[str, str]) -> str:
    """One `host` block per host, pointing its `modules.v1` service at the given https URL.

    Raises:
        CliConfigError: A host is not a plain hostname, or a URL is not https or holds
            an HCL template sequence.
    """
    blocks: list[str] = []
    for host, url in sorted(module_hosts.items()):
        if not HOST_PATTERN.match(host):
            raise CliConfigError(f"{host!r} is not a registry hostname")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or "${" in url or "%{" in url:
            raise CliConfigError(f"the modules service for {host} is not an https URL")
        blocks.append(f'host {json.dumps(host)} {{\n  services = {{\n    "modules.v1" = {json.dumps(url)}\n  }}\n}}\n')
    return "\n".join(blocks)


def write(directory: Path, module_hosts: Mapping[str, str], group: int | None = None) -> dict[str, str]:
    """Write the config under `directory` for the engine to read, returning the variable naming it.

    Empty when there is no host to map, so the engine keeps its default discovery.
    """
    if not module_hosts:
        return {}
    private_directory(directory, group)
    path = directory / FILE_NAME
    write_private(path, render(module_hosts), group)
    return {CONFIG_VARIABLE: str(path)}
