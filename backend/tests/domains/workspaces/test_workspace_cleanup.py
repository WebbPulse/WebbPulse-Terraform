"""The workspace delete cascade: rows in the request, objects inline or through the cleanup queue.

Both buckets are versioned here, as the stack makes them, so each test proves every
version and delete marker goes, not just the current object, and that another
workspace's objects under neighbouring prefixes are never touched.
"""

from __future__ import annotations

import json
import os

import boto3
import pytest

from app.common.composition import settings as settings_module
from app.common.db import repositories
from app.common.workspaces import cleanup
from app.domains.runs.consumers import dispatch
from tests.conftest import ARTIFACTS_BUCKET, REGION, STATE_BUCKET

BASE = "/api/v1/workspaces"
RUN_ONE = "run-01JBQ0000000000000000000C1"
RUN_TWO = "run-01JBQ0000000000000000000C2"
OTHER_RUN = "run-01JBQ0000000000000000000C9"


def s3():
    """An S3 client on the mocked region."""
    return boto3.client("s3", region_name=REGION)


def version_both_buckets() -> None:
    """Turn on versioning for the state and artifacts buckets."""
    for bucket in (STATE_BUCKET, ARTIFACTS_BUCKET):
        s3().put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})


def put_with_history(bucket: str, key: str) -> None:
    """Two versions and a delete marker, then a live version again."""
    s3().put_object(Bucket=bucket, Key=key, Body=b"one")
    s3().put_object(Bucket=bucket, Key=key, Body=b"two")
    s3().delete_object(Bucket=bucket, Key=key)
    s3().put_object(Bucket=bucket, Key=key, Body=b"three")


def remaining(bucket: str, prefix: str) -> int:
    """How many versions and delete markers sit under a prefix."""
    listing = s3().list_object_versions(Bucket=bucket, Prefix=prefix)
    return len(listing.get("Versions", [])) + len(listing.get("DeleteMarkers", []))


def seed_run(workspace_id: str, run_id: str) -> None:
    """One finished run row."""
    repositories.runs().put(
        {"run_id": run_id, "workspace_id": workspace_id, "status": "applied", "created_at": "2026-09-25T00:00:00Z"}
    )


def seed_objects(workspace_id: str, run_ids: list[str]) -> None:
    """Run artifacts, a config tarball, state with history and a lock for the workspace."""
    for run_id in run_ids:
        seed_run(workspace_id, run_id)
        put_with_history(ARTIFACTS_BUCKET, f"runs/{run_id}/plan.json")
        s3().put_object(Bucket=ARTIFACTS_BUCKET, Key=f"runs/{run_id}/apply.log", Body=b"log")
    put_with_history(ARTIFACTS_BUCKET, f"configs/{workspace_id}/cv-1.tar.gz")
    put_with_history(STATE_BUCKET, f"workspaces/{workspace_id}/terraform.tfstate")
    s3().put_object(Bucket=STATE_BUCKET, Key=f"workspaces/{workspace_id}/terraform.tfstate.tflock", Body=b"{}")


def seed_config_version(auth_client, workspace_id: str) -> str:
    """One config version row through the API."""
    response = auth_client.post(f"{BASE}/{workspace_id}/config-versions", json={"size_bytes": 10})
    assert response.status_code == 201, response.text
    return response.json()["config_version"]["config_version_id"]


def seed_neighbour() -> None:
    """Objects of a workspace whose id shares a prefix with none of the deleted one."""
    s3().put_object(Bucket=ARTIFACTS_BUCKET, Key=f"runs/{OTHER_RUN}/plan.json", Body=b"x")
    s3().put_object(Bucket=ARTIFACTS_BUCKET, Key="configs/ws-neighbour/cv.tar.gz", Body=b"x")
    s3().put_object(Bucket=STATE_BUCKET, Key="workspaces/ws-neighbour/terraform.tfstate", Body=b"{}")


def assert_neighbour_untouched() -> None:
    """The neighbour's objects all still read back."""
    assert remaining(ARTIFACTS_BUCKET, f"runs/{OTHER_RUN}/") == 1
    assert remaining(ARTIFACTS_BUCKET, "configs/ws-neighbour/") == 1
    assert remaining(STATE_BUCKET, "workspaces/ws-neighbour/") == 1


def assert_purged(workspace_id: str, run_ids: list[str]) -> None:
    """Nothing of the workspace is left in either bucket."""
    for run_id in run_ids:
        assert remaining(ARTIFACTS_BUCKET, f"runs/{run_id}/") == 0
    assert remaining(ARTIFACTS_BUCKET, f"configs/{workspace_id}/") == 0
    assert remaining(STATE_BUCKET, f"workspaces/{workspace_id}/") == 0


def rows_left(workspace_id: str, config_version_id: str) -> dict[str, bool]:
    """Which of the workspace's rows still exist."""
    return {
        "workspace": repositories.workspaces().get({"workspace_id": workspace_id}) is not None,
        "config_version": repositories.config_versions().get({"config_version_id": config_version_id}) is not None,
        "variable": repositories.variables().get({"workspace_id": workspace_id, "key": "region"}) is not None,
        "run": repositories.runs().get({"run_id": RUN_ONE}) is not None,
    }


def add_variable(auth_client, workspace_id: str) -> None:
    """Set one plain variable on the workspace."""
    response = auth_client.put(
        f"{BASE}/{workspace_id}/variables/region",
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    assert response.status_code in (200, 201), response.text


@pytest.fixture
def cleanup_queue():
    """A moto queue the delete sends cleanup messages to."""
    client = boto3.client("sqs", region_name=REGION)
    url = client.create_queue(QueueName="workspace-cleanup")["QueueUrl"]
    os.environ["WORKSPACE_CLEANUP_QUEUE_URL"] = url
    settings_module.reset_settings_cache()
    yield url
    os.environ.pop("WORKSPACE_CLEANUP_QUEUE_URL", None)
    settings_module.reset_settings_cache()


def drain(url: str) -> list[dict]:
    """Every message on the queue, as Lambda would deliver them, removed from the queue."""
    client = boto3.client("sqs", region_name=REGION)
    records: list[dict] = []
    while True:
        batch = client.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
        if not batch:
            return records
        for message in batch:
            records.append({"body": message["Body"]})
            client.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])


def test_inline_delete_removes_rows_and_every_object_version(auth_client, workspace):
    """With no queue, the delete purges every version, marker, tarball and lock itself."""
    version_both_buckets()
    workspace_id = workspace["workspace_id"]
    config_version_id = seed_config_version(auth_client, workspace_id)
    add_variable(auth_client, workspace_id)
    seed_objects(workspace_id, [RUN_ONE, RUN_TWO])
    seed_neighbour()
    response = auth_client.delete(f"{BASE}/{workspace_id}", params={"force": "true"})
    assert response.status_code == 204, response.text
    assert rows_left(workspace_id, config_version_id) == dict.fromkeys(
        ("workspace", "config_version", "variable", "run"), False
    )
    assert_purged(workspace_id, [RUN_ONE, RUN_TWO])
    assert_neighbour_untouched()


def test_queued_delete_removes_rows_now_and_objects_on_consume(auth_client, workspace, cleanup_queue):
    """With a queue, rows go in the request and the objects go when the runs function consumes."""
    version_both_buckets()
    workspace_id = workspace["workspace_id"]
    config_version_id = seed_config_version(auth_client, workspace_id)
    add_variable(auth_client, workspace_id)
    seed_objects(workspace_id, [RUN_ONE, RUN_TWO])
    seed_neighbour()
    response = auth_client.delete(f"{BASE}/{workspace_id}", params={"force": "true"})
    assert response.status_code == 204, response.text
    assert not any(rows_left(workspace_id, config_version_id).values())
    assert remaining(STATE_BUCKET, f"workspaces/{workspace_id}/") > 0
    records = drain(cleanup_queue)
    assert [sorted(json.loads(record["body"])["run_ids"]) for record in records] == [[RUN_ONE, RUN_TWO]]
    for record in records:
        dispatch.route_record(record)
    assert_purged(workspace_id, [RUN_ONE, RUN_TWO])
    assert_neighbour_untouched()
    for record in records:
        dispatch.route_record(record)


def test_consumer_refuses_while_a_failed_delete_left_the_workspace(auth_client, workspace, cleanup_queue, monkeypatch):
    """A delete that fails after sending leaves the workspace, and its message purges nothing."""
    from app.domains.workspaces import service

    workspace_id = workspace["workspace_id"]
    seed_objects(workspace_id, [RUN_ONE])

    def failing(*args, **kwargs):
        """Fail the runs delete, as a run turning active mid delete would."""
        raise RuntimeError("simulated")

    monkeypatch.setattr(service, "delete_workspace_runs", failing)
    with pytest.raises(RuntimeError):
        auth_client.delete(f"{BASE}/{workspace_id}", params={"force": "true"})
    records = drain(cleanup_queue)
    assert len(records) == 1
    with pytest.raises(cleanup.WorkspaceStillPresent):
        dispatch.route_record(records[0])
    assert remaining(STATE_BUCKET, f"workspaces/{workspace_id}/") > 0
    assert remaining(ARTIFACTS_BUCKET, f"runs/{RUN_ONE}/") > 0


def test_delete_refuses_while_a_run_is_active_and_sends_nothing(auth_client, workspace, cleanup_queue):
    """An unfinished run is a 409 before any message is sent."""
    workspace_id = workspace["workspace_id"]
    repositories.runs().put(
        {"run_id": RUN_ONE, "workspace_id": workspace_id, "status": "planning", "created_at": "2026-09-25T00:00:00Z"}
    )
    response = auth_client.delete(f"{BASE}/{workspace_id}", params={"force": "true"})
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "WORKSPACE_HAS_ACTIVE_RUN"
    assert drain(cleanup_queue) == []


def test_consumer_continues_in_a_new_message_when_the_budget_runs_out(cleanup_queue, monkeypatch):
    """Past the budget, one run is purged and the rest ride a continuation message."""
    workspace_id = "ws-gone"
    for run_id in (RUN_ONE, RUN_TWO):
        s3().put_object(Bucket=ARTIFACTS_BUCKET, Key=f"runs/{run_id}/plan.json", Body=b"x")
    s3().put_object(Bucket=STATE_BUCKET, Key=f"workspaces/{workspace_id}/terraform.tfstate", Body=b"{}")
    monkeypatch.setattr(cleanup, "CONSUMER_BUDGET_SECONDS", 0.0)
    body = {"kind": cleanup.CLEANUP_KIND, "workspace_id": workspace_id, "run_ids": [RUN_ONE, RUN_TWO]}
    cleanup.handle_record({"body": json.dumps(body)})
    assert remaining(ARTIFACTS_BUCKET, f"runs/{RUN_ONE}/") == 0
    assert remaining(ARTIFACTS_BUCKET, f"runs/{RUN_TWO}/") == 1
    assert remaining(STATE_BUCKET, f"workspaces/{workspace_id}/") == 1
    records = drain(cleanup_queue)
    assert [json.loads(record["body"])["run_ids"] for record in records] == [[RUN_TWO]]
    cleanup.handle_record(records[0])
    assert_purged(workspace_id, [RUN_ONE, RUN_TWO])
    assert drain(cleanup_queue) == []


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps(["a list"]),
        json.dumps({"kind": "other", "workspace_id": "ws-x", "run_ids": []}),
        json.dumps({"kind": cleanup.CLEANUP_KIND, "run_ids": []}),
        json.dumps({"kind": cleanup.CLEANUP_KIND, "workspace_id": "", "run_ids": []}),
        json.dumps({"kind": cleanup.CLEANUP_KIND, "workspace_id": "run-x", "run_ids": []}),
        json.dumps({"kind": cleanup.CLEANUP_KIND, "workspace_id": "ws-x", "run_ids": "run-1"}),
        json.dumps({"kind": cleanup.CLEANUP_KIND, "workspace_id": "ws-x", "run_ids": [""]}),
        json.dumps({"kind": cleanup.CLEANUP_KIND, "workspace_id": "ws-x", "run_ids": [3]}),
    ],
)
def test_malformed_bodies_are_refused(body):
    """A body the consumer cannot act on raises, so it parks on the dead letter queue."""
    with pytest.raises(cleanup.MalformedCleanup):
        cleanup.handle_record({"body": body})


def test_messages_split_run_ids_and_always_send_one(monkeypatch):
    """Run ids are chunked, and a workspace with no runs still gets one message."""
    monkeypatch.setattr(cleanup, "RUN_IDS_PER_MESSAGE", 2)
    assert [body["run_ids"] for body in cleanup.messages("ws-a", ["r1", "r2", "r3"])] == [["r1", "r2"], ["r3"]]
    assert cleanup.messages("ws-a", []) == [{"kind": cleanup.CLEANUP_KIND, "workspace_id": "ws-a", "run_ids": []}]


def test_enqueue_without_a_queue_sends_nothing():
    """No configured queue means the caller purges inline."""
    assert cleanup.enqueue("ws-a", ["r1"]) is False


def test_purge_prefix_handles_an_unversioned_bucket_and_no_bucket():
    """Plain objects are deleted too, and an unset bucket is a no-op."""
    s3().put_object(Bucket=STATE_BUCKET, Key="workspaces/ws-a/terraform.tfstate", Body=b"{}")
    s3().put_object(Bucket=STATE_BUCKET, Key="workspaces/ws-ab/terraform.tfstate", Body=b"{}")
    resolved = settings_module.get_settings()
    assert cleanup.purge_prefix(STATE_BUCKET, "workspaces/ws-a/", settings=resolved) == 1
    assert remaining(STATE_BUCKET, "workspaces/ws-a/") == 0
    assert remaining(STATE_BUCKET, "workspaces/ws-ab/") == 1
    assert cleanup.purge_prefix("", "workspaces/ws-a/", settings=resolved) == 0
