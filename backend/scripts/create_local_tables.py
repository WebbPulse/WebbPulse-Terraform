"""Create every DynamoDB table against a local DynamoDB.

No product table carries a TTL attribute: a workspace, a variable, a config version,
a run and a user are all deleted deliberately or kept, so there is nothing to
expire. The identity module's own tables, several of which do expire, are created by
Terraform in a deployed environment and by this script locally, so a local stack can
sign a user in without a deploy.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.common.db.tables import ALL_TABLES, local_table_name, table_definition  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse the environment, endpoint and region for the local DynamoDB."""
    parser = argparse.ArgumentParser(description="Create DynamoDB tables locally")
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT", "development"))
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("DYNAMODB_ENDPOINT_URL", "http://localhost:8002"),
    )
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    parser.add_argument(
        "--identity-table-prefix",
        default=os.environ.get("IDENTITY_TABLE_PREFIX", ""),
        help="Prefix the identity module's tables carry. Empty derives it from the environment.",
    )
    return parser.parse_args()


def _create(client: Any, physical: str, definition: dict[str, Any]) -> bool:
    """Create one table, returning whether it was new rather than already present."""
    try:
        client.create_table(**definition)
        client.get_waiter("table_exists").wait(TableName=physical)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise
        print(f"exists  {physical}")
        return False
    print(f"created {physical}")
    return True


def create_identity_tables(prefix: str, endpoint_url: str, region: str) -> list[str]:
    """Create the identity module's own tables, which Terraform owns in a deployed stack.

    A local stack has no Terraform, so without these a login has nowhere to read a
    credential from and the whole identity surface is unusable.
    """
    from webbpulse.identity import storage

    client = boto3.client("dynamodb", endpoint_url=endpoint_url, region_name=region)
    created = []
    for spec in storage.TABLES:
        definition = spec.create_table_request(prefix)
        ttl = definition.pop("TimeToLiveSpecification", None)
        physical = definition["TableName"]
        if _create(client, physical, definition) and ttl is not None:
            try:
                client.update_time_to_live(TableName=physical, TimeToLiveSpecification=ttl)
            except ClientError:
                pass
        if physical not in created and definition:
            created.append(physical)
    return created


def create_tables(environment: str, endpoint_url: str, region: str) -> list[str]:
    """Create any missing table, leaving existing ones alone so reruns are safe."""
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    client = boto3.client("dynamodb", endpoint_url=endpoint_url, region_name=region)
    created = []
    for logical in ALL_TABLES:
        physical = local_table_name(logical, environment)
        definition = table_definition(logical, physical)
        try:
            client.create_table(**definition)
            client.get_waiter("table_exists").wait(TableName=physical)
            created.append(physical)
            print(f"created {physical}")
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceInUseException":
                raise
            print(f"exists  {physical}")
    return created


def main() -> None:
    """Create the product and identity tables and report how many were new."""
    args = parse_args()
    created = create_tables(args.environment, args.endpoint_url, args.region)
    print(f"{len(created)} table(s) created, {len(ALL_TABLES) - len(created)} already present")

    prefix = args.identity_table_prefix or f"webbpulse-terraform-{args.environment}"
    identity = create_identity_tables(prefix, args.endpoint_url, args.region)
    print(f"{len(identity)} identity table(s) present under {prefix}")


if __name__ == "__main__":
    main()
