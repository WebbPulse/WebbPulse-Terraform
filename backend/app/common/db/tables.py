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

WORKSPACES_BY_NAME_INDEX: Final = "by_name"
"""The GSI enforcing one workspace per name, and resolving a name to a workspace."""

RUNS_BY_WORKSPACE_INDEX: Final = "by_workspace"
"""The GSI listing one workspace's runs, newest last by `created_at`."""

CONFIG_VERSIONS_BY_WORKSPACE_INDEX: Final = "by_workspace"
"""The GSI listing one workspace's config versions, newest last by `created_at`."""

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
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": WORKSPACES_BY_NAME_INDEX,
                "KeySchema": [{"AttributeName": "name", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    },
    RUNS: {
        "BillingMode": "PAY_PER_REQUEST",
        "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "workspace_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": RUNS_BY_WORKSPACE_INDEX,
                "KeySchema": [
                    {"AttributeName": "workspace_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
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
}

ALL_TABLES: Final = (WORKSPACES, RUNS, VARIABLES, CONFIG_VERSIONS)
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
