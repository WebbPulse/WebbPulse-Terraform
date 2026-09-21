"""Legacy runs enter the recency index without changing provenance or other data."""

import boto3

from app.common.db.tables import RUNS, local_table_name
from scripts.backfill_run_collection import backfill
from tests.conftest import ENVIRONMENT, REGION


def test_backfill_is_bounded_resumable_and_idempotent(auth_client, created_run):
    """Dry runs only count; resumed writes index old rows and preserve actor null."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    original = table.get_item(Key={"run_id": created_run["run_id"]}).get("Item", {})
    legacy = {key: value for key, value in original.items() if key != "collection" and not key.startswith("actor_")}
    table.put_item(Item=legacy)
    assert auth_client.get("/api/v1/runs").json()["items"] == []
    dry = backfill(table)
    assert dry["eligible"] == 1
    assert dry["updated"] == 0
    assert table.get_item(Key={"run_id": legacy["run_id"]}).get("Item") == legacy

    after = None
    updated = 0
    for _ in range(10):
        result = backfill(table, write=True, page_size=1, max_pages=1, after=after)
        assert result["scanned"] <= 1
        updated += result["updated"]
        after = result["next_after"]
        if after is None:
            break
    assert after is None
    assert updated == 1
    assert table.get_item(Key={"run_id": legacy["run_id"]}).get("Item") == {**legacy, "collection": "run"}
    listed = auth_client.get("/api/v1/runs").json()["items"]
    assert [run["run_id"] for run in listed] == [legacy["run_id"]]
    assert listed[0]["actor"] is None
    assert backfill(table, write=True)["updated"] == 0
    semaphore = table.get_item(Key={"run_id": "run-semaphore"}).get("Item", {})
    assert "collection" not in semaphore


def test_backfill_does_not_recreate_a_concurrently_deleted_run(created_run, monkeypatch):
    """The conditional update cannot resurrect a row deleted after the scan."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    key = {"run_id": created_run["run_id"]}
    table.update_item(
        Key=key, UpdateExpression="REMOVE #collection", ExpressionAttributeNames={"#collection": "collection"}
    )
    update = table.update_item

    def delete_before_update(**kwargs):
        """Simulate deletion between reading the scan page and updating its row."""
        table.delete_item(Key=key)
        return update(**kwargs)

    monkeypatch.setattr(table, "update_item", delete_before_update)
    result = backfill(table, write=True)
    assert result["updated"] == 0
    assert "Item" not in table.get_item(Key=key)
