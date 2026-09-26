"""Every DynamoDB table this control plane owns, and its CreateTable spec.

One registry, so the test suite, the local table script and the Terraform stack
describe the same shapes. Table names come from the environment rather than from
a prefix, because the stack names each table explicitly in the contract.
"""

from __future__ import annotations

from typing import Any, Final

WORKSPACES: Final = "workspaces"
RUNS: Final = "runs"
VARIABLES: Final = "variables"
CONFIG_VERSIONS: Final = "config-versions"
USERS: Final = "users"
GITHUB: Final = "github"
VCS_UPLOADS: Final = "vcs-uploads"

WORKSPACES_BY_NAME_INDEX: Final = "by_name"
"""The GSI enforcing one workspace per name, and resolving a name to a workspace."""

WORKSPACES_BY_VCS_REPO_INDEX: Final = "by_vcs_repo"
"""The GSI finding the workspaces bound to a repository by its lowercased `owner/name`."""

WORKSPACES_BY_VCS_REPOSITORY_ID_INDEX: Final = "by_vcs_repository_id"
"""The GSI finding the workspaces bound to a repository by its GitHub id, which a
rename leaves unchanged."""

VCS_UPLOADS_TTL_ATTRIBUTE: Final = "expires_at"
"""The epoch seconds attribute DynamoDB expires an ingest record on."""

RUNS_BY_WORKSPACE_INDEX: Final = "by_workspace"
"""The GSI listing one workspace's runs, newest last by `created_at`."""

RUNS_BY_RECENCY_INDEX: Final = "by_recency"
"""The GSI listing every workspace's runs together, newest last by `run_id`.

Partitioned on the constant `collection` attribute, since a cross-workspace list has
no natural key to group on and run volume sits far under one partition's ceiling.
`run_id` is a ULID, so it orders by creation time on its own.
"""

RUNS_COLLECTION: Final = "run"
"""The one value `collection` holds. Stamped on every run row at create and never
on the semaphore row, so the reserved row stays out of `by_recency`."""

CONFIG_VERSIONS_BY_WORKSPACE_INDEX: Final = "by_workspace"
"""The GSI listing one workspace's config versions, newest last by `created_at`."""

USERS_BY_EMAIL_INDEX: Final = "email_lower-index"
"""The GSI resolving a lowercased address to its user, which is how sign-in looks one up."""

SEMAPHORE_RUN_ID: Final = "run-semaphore"
"""The runs table row holding the environment-wide concurrency count.

A row in the runs table rather than a table of its own, because a single counter
does not justify a table and the contract pins it here.
"""

_SPECS: Final[dict[str, dict[str, Any]]] = {
    WORKSPACES: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "workspace_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "workspace_id", "AttributeType": "S"},
            {"AttributeName": "name", "AttributeType": "S"},
            {"AttributeName": "vcs_repo_key", "AttributeType": "S"},
            {"AttributeName": "vcs_repository_id", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": WORKSPACES_BY_NAME_INDEX,
                "KeySchema": [{"AttributeName": "name", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": WORKSPACES_BY_VCS_REPO_INDEX,
                "KeySchema": [{"AttributeName": "vcs_repo_key", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": WORKSPACES_BY_VCS_REPOSITORY_ID_INDEX,
                "KeySchema": [{"AttributeName": "vcs_repository_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    },
    RUNS: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "workspace_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
            {"AttributeName": "collection", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": RUNS_BY_WORKSPACE_INDEX,
                "KeySchema": [
                    {"AttributeName": "workspace_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": RUNS_BY_RECENCY_INDEX,
                "KeySchema": [
                    {"AttributeName": "collection", "KeyType": "HASH"},
                    {"AttributeName": "run_id", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    },
    VARIABLES: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [
            {"AttributeName": "workspace_id", "KeyType": "HASH"},
            {"AttributeName": "key", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "workspace_id", "AttributeType": "S"},
            {"AttributeName": "key", "AttributeType": "S"},
        ],
    },
    CONFIG_VERSIONS: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "config_version_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "config_version_id", "AttributeType": "S"},
            {"AttributeName": "workspace_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
                "KeySchema": [
                    {"AttributeName": "workspace_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    USERS: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "email_lower", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": USERS_BY_EMAIL_INDEX,
                "KeySchema": [{"AttributeName": "email_lower", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    GITHUB: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
    },
    VCS_UPLOADS: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "upload_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [{"AttributeName": "upload_id", "AttributeType": "S"}],
    },
}

GITHUB_TTL_ATTRIBUTE: Final = "expires_at"
"""The GitHub table's TTL attribute. Terraform enables it; a read checks it too,
because DynamoDB deletes expired rows up to days late."""

ALL_TABLES: Final = (WORKSPACES, RUNS, VARIABLES, CONFIG_VERSIONS, USERS, GITHUB, VCS_UPLOADS)
"""Every logical table, in creation order. The suite and the local script walk it."""


def local_table_name(logical_name: str, environment: str = "development") -> str:
    """The name a table takes when nothing in the environment has named it.

    The contract names each table `webbpulse-terraform-<env>-<logical>`, and the
    local script and the suite derive the same shape rather than restating it.
    """
    return f"webbpulse-terraform-{environment}-{logical_name}"


def table_definition(logical_name: str, physical_name: str) -> dict[str, Any]:
    """A CreateTable request for one logical table under a physical name."""
    spec = {key: value for key, value in _SPECS[logical_name].items()}
    spec["TableName"] = physical_name
    return spec
