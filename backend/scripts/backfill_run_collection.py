"""One-off: stamp `collection = "run"` on runs written before `by_recency` existed.

A GSI only holds items carrying its key attributes, and rows created before this
change lack `collection`, so they are missing from the cross-workspace list until
this runs. It sets that one attribute and nothing else: legacy runs keep no actor,
because who triggered them was never recorded.

Run by hand from `backend/` once the index is ACTIVE and the backend that stamps
new rows is deployed. Dry run by default; `--write` applies conditional updates,
so a rerun or an overlapping run changes nothing twice. Each call scans at most
`--max-pages` pages; continue with `--after NEXT_AFTER` until it prints null.

    uv run python scripts/backfill_run_collection.py --table TABLE --region us-west-2 [--write]
"""

import argparse
import json
import re
from itertools import islice
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr


def backfill(
    table: Any,
    *,
    write: bool = False,
    page_size: int = 100,
    max_pages: int = 10,
    after: str | None = None,
) -> dict[str, Any]:
    """Process bounded scan pages and return a restart key plus non-sensitive counts."""
    if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 100:
        raise ValueError("page_size must be 1..1000 and max_pages must be 1..100")
    arguments: dict[str, Any] = {
        "TableName": table.name,
        "ProjectionExpression": "run_id, workspace_id, #collection",
        "ExpressionAttributeNames": {"#collection": "collection"},
        "ConsistentRead": True,
        "PaginationConfig": {"PageSize": page_size},
    }
    if after is not None:
        arguments["ExclusiveStartKey"] = {"run_id": after}
    pages = table.meta.client.get_paginator("scan").paginate(**arguments)
    result: dict[str, Any] = {"scanned": 0, "eligible": 0, "updated": 0, "next_after": None}
    for page in islice(pages, max_pages):
        result["scanned"] += page["ScannedCount"]
        for item in page.get("Items", []):
            run_id = item.get("run_id", "")
            if (
                not re.fullmatch(r"run-[0-9A-HJKMNP-TV-Z]{26}", run_id)
                or not item.get("workspace_id")
                or "collection" in item
            ):
                continue
            result["eligible"] += 1
            if not write:
                continue
            try:
                table.update_item(
                    Key={"run_id": run_id},
                    UpdateExpression="SET #collection = :collection",
                    ExpressionAttributeNames={"#collection": "collection"},
                    ExpressionAttributeValues={":collection": "run"},
                    ConditionExpression=Attr("run_id").exists()
                    & Attr("workspace_id").exists()
                    & Attr("collection").not_exists(),
                )
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                continue
            result["updated"] += 1
        result["next_after"] = page.get("LastEvaluatedKey", {}).get("run_id")
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
