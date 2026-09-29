"""One-off: stamp the `purge_at` TTL on dead run-scoped keys written before revokes set it.

A run key is dead once it is revoked or past its `expires_at`. Its TTL is that moment
plus `RUN_KEY_RETENTION`, so a key that died long ago gets a TTL in the past and
DynamoDB removes it within a day or two. Live run keys and agent keys are skipped.

Run by hand from `backend/` once the table's TTL is enabled. Dry run by default;
`--write` applies conditional updates, so a rerun or an overlapping run changes
nothing twice. Each call scans at most `--max-pages` pages; continue with
`--after NEXT_AFTER` until it prints null.

    uv run python -m scripts.backfill_run_key_ttl --table TABLE --region us-west-2 [--write]
"""

import argparse
import json
from datetime import datetime, timezone
from itertools import islice
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

from app.common.core.auth import RUN_KEY_RETENTION, RUN_KEY_TTL_ATTRIBUTE, RUN_KEY_USER_PREFIX


def died_at(item: dict[str, Any], now: datetime) -> datetime | None:
    """When a run key stopped working: its revocation, else a past expiry, else `None`."""
    revoked = str(item.get("revoked_at", "") or "")
    if revoked:
        parsed = datetime.fromisoformat(revoked.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    expires = int(item.get("expires_at", 0) or 0)
    if expires and expires <= now.timestamp():
        return datetime.fromtimestamp(expires, timezone.utc)
    return None


def backfill(
    table: Any,
    *,
    write: bool = False,
    page_size: int = 100,
    max_pages: int = 10,
    after: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Process bounded scan pages and return a restart key plus non-sensitive counts."""
    if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 100:
        raise ValueError("page_size must be 1..1000 and max_pages must be 1..100")
    moment = now or datetime.now(timezone.utc)
    arguments: dict[str, Any] = {
        "TableName": table.name,
        "ProjectionExpression": "key_hash, user_id, revoked_at, expires_at, #purge",
        "ExpressionAttributeNames": {"#purge": RUN_KEY_TTL_ATTRIBUTE},
        "ConsistentRead": True,
        "PaginationConfig": {"PageSize": page_size},
    }
    if after is not None:
        arguments["ExclusiveStartKey"] = {"key_hash": after}
    pages = table.meta.client.get_paginator("scan").paginate(**arguments)
    result: dict[str, Any] = {"scanned": 0, "eligible": 0, "updated": 0, "next_after": None}
    for page in islice(pages, max_pages):
        result["scanned"] += page["ScannedCount"]
        for item in page.get("Items", []):
            if not str(item.get("user_id", "")).startswith(RUN_KEY_USER_PREFIX) or RUN_KEY_TTL_ATTRIBUTE in item:
                continue
            dead = died_at(item, moment)
            if dead is None:
                continue
            result["eligible"] += 1
            if not write:
                continue
            try:
                table.update_item(
                    Key={"key_hash": item["key_hash"]},
                    UpdateExpression="SET #purge = :purge",
                    ExpressionAttributeNames={"#purge": RUN_KEY_TTL_ATTRIBUTE},
                    ExpressionAttributeValues={":purge": int((dead + RUN_KEY_RETENTION).timestamp())},
                    ConditionExpression=Attr("key_hash").exists()
                    & Attr("user_id").begins_with(RUN_KEY_USER_PREFIX)
                    & Attr(RUN_KEY_TTL_ATTRIBUTE).not_exists(),
                )
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
            result["updated"] += 1
        result["next_after"] = page.get("LastEvaluatedKey", {}).get("key_hash")
    return result


def main() -> None:
    """Require an explicit table and region; only --write enables conditional updates."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--after")
    args = parser.parse_args()
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    print(
        json.dumps(
            backfill(table, write=args.write, page_size=args.page_size, max_pages=args.max_pages, after=args.after)
        )
    )


if __name__ == "__main__":
    main()
