"""Revoked run-scoped keys carry the TTL that removes them, and agent keys never do."""

from datetime import datetime, timedelta, timezone

import boto3
from webbpulse.identity.api_keys import mint

from app.common.core.auth import (
    RUN_KEY_RETENTION,
    RUN_KEY_TTL_ATTRIBUTE,
    RUN_TOKEN_TENANT,
    RUNNER_SCOPE,
    api_key_store,
    revoke_run_key,
)
from app.common.db.tables import RUNS, local_table_name
from app.domains.runs import service as runs_service
from scripts.backfill_run_key_ttl import backfill
from tests.conftest import ENVIRONMENT, REGION, TABLE_PREFIX


def api_keys_table():
    """The moto `api-keys` table."""
    return boto3.resource("dynamodb", region_name=REGION).Table(f"{TABLE_PREFIX}-api-keys")


def stored_key(key_hash: str) -> dict:
    """The raw api-keys row."""
    return api_keys_table().get_item(Key={"key_hash": key_hash}).get("Item", {})


def mint_hash(user_id: str, *, expires_at: datetime | None = None) -> str:
    """Mint a key for `user_id` and return its hash."""
    return mint(
        user_id=user_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=(RUNNER_SCOPE,),
        expires_at=expires_at,
        store=api_key_store(),
    ).record.key_hash


def test_a_terminal_transition_stamps_the_run_token_ttl(runner_client, created_run):
    """The token a finished run held is revoked and scheduled for deletion a day later."""
    run_id = created_run["run_id"]
    runs_table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    key_hash = str(runs_table.get_item(Key={"run_id": run_id}).get("Item", {})["run_token_hash"])
    before = datetime.now(timezone.utc)

    runs_service.finish_run(run_id, "applied")

    row = stored_key(key_hash)
    assert row["revoked_at"]
    purge_at = int(row[RUN_KEY_TTL_ATTRIBUTE])
    assert int((before + RUN_KEY_RETENTION).timestamp()) <= purge_at
    assert purge_at <= int((datetime.now(timezone.utc) + RUN_KEY_RETENTION).timestamp())


def test_revoke_run_key_leaves_agent_keys_without_a_ttl():
    """An agent key revoked through the run path is revoked but never scheduled for deletion."""
    key_hash = mint_hash("user-agent")
    revoke_run_key(key_hash)
    row = stored_key(key_hash)
    assert row["revoked_at"]
    assert RUN_KEY_TTL_ATTRIBUTE not in row


def test_revoke_run_key_stamps_an_already_revoked_key_and_creates_nothing():
    """A key revoked before the TTL existed still gets it, and a missing row stays missing."""
    key_hash = mint_hash("run-01J00000000000000000000000")
    api_key_store().revoke(key_hash)
    revoke_run_key(key_hash)
    assert RUN_KEY_TTL_ATTRIBUTE in stored_key(key_hash)

    revoke_run_key("missing-hash")
    assert stored_key("missing-hash") == {}


def test_backfill_stamps_dead_run_keys_only():
    """Revoked and expired run keys get a TTL from when they died; live and agent keys do not."""
    now = datetime.now(timezone.utc)
    revoked = mint_hash("run-01J00000000000000000000001")
    revoked_at = now - timedelta(days=10)
    api_key_store().revoke(revoked, revoked_at=revoked_at.isoformat())
    expired_at = now - timedelta(days=3)
    expired = mint_hash("run-01J00000000000000000000002", expires_at=expired_at)
    live = mint_hash("run-01J00000000000000000000003", expires_at=now + timedelta(hours=1))
    agent = mint_hash("user-agent")
    api_key_store().revoke(agent)
    table = api_keys_table()

    dry = backfill(table, now=now)
    assert dry["eligible"] == 2
    assert dry["updated"] == 0
    assert RUN_KEY_TTL_ATTRIBUTE not in stored_key(revoked)

    after = None
    updated = 0
    for _ in range(10):
        result = backfill(table, write=True, page_size=1, max_pages=1, after=after, now=now)
        updated += result["updated"]
        after = result["next_after"]
        if after is None:
            break
    assert after is None
    assert updated == 2
    assert int(stored_key(revoked)[RUN_KEY_TTL_ATTRIBUTE]) == int((revoked_at + RUN_KEY_RETENTION).timestamp())
    assert int(stored_key(expired)[RUN_KEY_TTL_ATTRIBUTE]) == int(expired_at.timestamp()) + int(
        RUN_KEY_RETENTION.total_seconds()
    )
    assert RUN_KEY_TTL_ATTRIBUTE not in stored_key(live)
    assert RUN_KEY_TTL_ATTRIBUTE not in stored_key(agent)
    assert backfill(table, write=True, now=now)["updated"] == 0
