"""Fixed facts about each control plane environment and the identifiers the tool accepts."""

from __future__ import annotations

import re
from dataclasses import dataclass

HCP_HOST = "app.terraform.io"
HCP_API = f"https://{HCP_HOST}/api/v2"

ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
HCP_WORKSPACE_ID = re.compile(r"^ws-[A-Za-z0-9]{16}$")
PLANE_WORKSPACE_ID = re.compile(r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")
ENGINE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class UsageError(Exception):
    """An argument the tool refuses before touching anything."""


@dataclass(frozen=True)
class Environment:
    """One control plane environment: its API, its state bucket and the profile that writes there."""

    name: str
    host: str
    api_url: str
    state_bucket: str
    kms_alias: str
    aws_profile: str
    region: str = "us-west-2"


ENVIRONMENTS: dict[str, Environment] = {
    "prod": Environment(
        name="prod",
        host="terraform.webbpulse.com",
        api_url="https://api.terraform.webbpulse.com",
        state_bucket="webbpulse-terraform-prod-state",
        kms_alias="alias/webbpulse-terraform-prod-state",
        aws_profile="WebbPulse-Terraform-Production/AdministratorAccess",
    ),
    "staging": Environment(
        name="staging",
        host="staging.terraform.webbpulse.com",
        api_url="https://api.staging.terraform.webbpulse.com",
        state_bucket="webbpulse-terraform-staging-state",
        kms_alias="alias/webbpulse-terraform-staging-state",
        aws_profile="WebbPulse-Terraform-Staging/AdministratorAccess",
    ),
}


def state_key(plane_workspace_id: str) -> str:
    """The S3 key the plane's runner reads and writes for a workspace's default state."""
    return f"workspaces/{check_plane_workspace(plane_workspace_id)}/terraform.tfstate"


def check_hcp_workspace(value: str) -> str:
    """`value` if it is an HCP workspace id, else a `UsageError`."""
    if not HCP_WORKSPACE_ID.match(value):
        raise UsageError(f"not an HCP workspace id: {value!r}")
    return value


def check_plane_workspace(value: str) -> str:
    """`value` if it is a plane workspace id, else a `UsageError`."""
    if not PLANE_WORKSPACE_ID.match(value):
        raise UsageError(f"not a control plane workspace id: {value!r}")
    return value


def check_issue_key(value: str) -> str:
    """`value` if it is a Standupless issue key such as TF-26, else a `UsageError`."""
    if not ISSUE_KEY.match(value):
        raise UsageError(f"not an issue key: {value!r}")
    return value


def check_engine_version(value: str) -> str:
    """`value` if it is an exact `X.Y.Z` engine version, else a `UsageError`."""
    if not ENGINE_VERSION.match(value):
        raise UsageError(f"not an exact engine version: {value!r}")
    return value
