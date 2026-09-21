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

WORKSPACES_BY_NAME_INDEX: Final = "by_name"
"""The GSI enforcing one workspace per name, and resolving a name to a workspace."""

RUNS_BY_WORKSPACE_INDEX: Final = "by_workspace"
"""The GSI listing one workspace's runs, newest last by `created_at`."""

RUNS_BY_RECENCY_INDEX: Final = "by_recency"
"""The GSI listing every workspace's runs together, newest last by `run_id`.

Partitioned on the constant `collection` attribute, because listing across
workspaces has no natural key to group on and the contract sizes this control
plane at about 25 runs on an active day with a peak of 73. That is four orders of
magnitude under the partition's own write ceiling, so the single partition a
constant key produces is the cheapest correct answer rather than a hot spot. The
sort key is `run_id`, which is a ULID and therefore already lexicographically
ordered by creation time, so recency needs no second attribute.

The projection is deliberately narrow: the workspace list renders a status badge
and a timestamp per row, so only the attributes that view reads are carried and
the index stays a fraction of the table.
"""

RUNS_COLLECTION: Final = "run"
"""The one value `collection` ever holds, which is what makes `by_recency` a single
partition. Stamped on every run row at create and never on the semaphore row, so
the reserved row stays out of the index the way it stays out of `by_workspace`."""

RUNS_BY_RECENCY_ATTRIBUTES: Final = (
    "run_id",
    "collection",
    "workspace_id",
    "config_version_id",
    "status",
    "created_at",
    "updated_at",
    "plan_only",
    "message",
    "changes",
    "actor_kind",
    "actor_id",
    "actor_display_name",
)
"""What `by_recency` projects, which is the smallest set that renders a list row.

Two constraints fix this set and neither is negotiable. The contract's `Run` model
requires `config_version_id`, so a projected row without it fails response
validation rather than rendering short. The list view itself reads `message` for
the run title, `changes` for the change counts, `status` for the badge,
`created_at` for the timestamp, `plan_only` for the kind and the actor columns for
the attribution.

Everything else a run carries stays off the index deliberately: the plan and apply
timestamps, the queue pointer, the error text, the execution ARN and both secrets
are read only on a single run's own page, which fetches the full row by id.
"""

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
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": [
                        name for name in RUNS_BY_RECENCY_ATTRIBUTES if name not in ("collection", "run_id")
                    ],
                },
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
}

ALL_TABLES: Final = (WORKSPACES, RUNS, VARIABLES, CONFIG_VERSIONS, USERS)
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
