"""The audit trail: what each change records, and the admin listing and export over it."""

from __future__ import annotations

import csv
import io
from typing import Any

from fastapi.testclient import TestClient
from webbpulse.audit import AuditQuery
from webbpulse.testing import assert_audit_log_contract

from app.common import audit
from app.common.core.auth import ADMIN, ALL_SCOPES, WORKSPACES_READ, WORKSPACES_WRITE
from tests.conftest import WORKSPACE_PAYLOAD, person_headers, seed_user

BASE = "/api/v1/audit-events"
PERSON = "user-auditor"


def _events(action: str | None = None) -> list[Any]:
    """Every recorded event, newest first, optionally of one action."""
    return audit.store().list_events(audit.AUDIT_TENANT, AuditQuery(action=action), limit=100).events


def _person(app: Any, *, scopes: tuple[str, ...] = ALL_SCOPES) -> TestClient:
    """A client for a signed-in person, fresh within the step-up window."""
    seed_user(PERSON)
    return TestClient(app, headers=person_headers(user_id=PERSON, scopes=scopes, roles=("admin",)))


def test_the_table_meets_the_audit_store_contract():
    """The plane's DynamoDB store behaves as every audit store must."""
    assert_audit_log_contract(audit.store())


def test_create_update_and_delete_are_recorded(auth_client):
    """A workspace's life leaves one event per change, each naming the workspace."""
    created = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD)
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace_id"]

    edited = auth_client.patch(f"/api/v1/workspaces/{workspace_id}", json={"description": "Changed."})
    assert edited.status_code == 200, edited.text
    resent = auth_client.patch(f"/api/v1/workspaces/{workspace_id}", json={"description": "Changed."})
    assert resent.status_code == 200, resent.text
    deleted = auth_client.delete(f"/api/v1/workspaces/{workspace_id}")
    assert deleted.status_code == 204, deleted.text

    [create] = _events(audit.WORKSPACE_CREATED)
    assert create.target.id == workspace_id
    assert create.target.label == WORKSPACE_PAYLOAD["name"]
    assert create.payload["engine"] == "terraform"
    assert create.actor.kind == "api_key"
    assert create.actor.source == "api"
    [update] = _events(audit.WORKSPACE_UPDATED)
    assert update.before == {"description": WORKSPACE_PAYLOAD["description"]}
    assert update.after == {"description": "Changed."}
    [delete] = _events(audit.WORKSPACE_DELETED)
    assert delete.payload == {"force": False}
    assert delete.target.label == WORKSPACE_PAYLOAD["name"]


def test_each_settings_group_is_its_own_action(app, workspace):
    """Plan access and remote state sharing each record under their own action."""
    workspace_id = workspace["workspace_id"]
    with _person(app) as client:
        response = client.patch(
            f"/api/v1/workspaces/{workspace_id}",
            json={
                "plan_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-plan",
                "global_remote_state": True,
            },
        )
    assert response.status_code == 200, response.text

    [plan] = _events(audit.PLAN_ACCESS_CHANGED)
    assert plan.after == {"plan_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-plan"}
    assert plan.actor.id == PERSON
    assert plan.actor.kind == "user"
    assert plan.actor.source == "web"
    [remote] = _events(audit.REMOTE_STATE_CHANGED)
    assert remote.after == {"global_remote_state": True}
    assert _events(audit.WORKSPACE_UPDATED) == []


def test_a_variable_write_records_its_key_and_category_never_its_value(auth_client, workspace):
    """The value stays out of the trail, sensitive or not."""
    workspace_id = workspace["workspace_id"]
    response = auth_client.put(
        f"/api/v1/workspaces/{workspace_id}/variables/region",
        json={"value": "us-west-2-distinctive", "category": "terraform"},
    )
    assert response.status_code == 200, response.text
    removed = auth_client.delete(f"/api/v1/workspaces/{workspace_id}/variables/region")
    assert removed.status_code == 204, removed.text

    [written] = _events(audit.VARIABLE_WRITTEN)
    assert written.payload == {"key": "region", "category": "terraform"}
    assert written.target.id == workspace_id
    [deleted] = _events(audit.VARIABLE_DELETED)
    assert deleted.payload == {"key": "region", "category": "terraform"}
    export = auth_client.get(f"{BASE}/export")
    assert "us-west-2-distinctive" not in export.text


def test_api_key_mint_and_revoke_are_recorded(app):
    """A key's mint and revoke name the key by its hash and never carry its plaintext."""
    with _person(app) as client:
        minted = client.post("/api/v1/api-keys", json={"name": "ci", "scopes": [WORKSPACES_READ]})
        assert minted.status_code == 201, minted.text
        key_id = minted.json()["key_id"]
        plaintext = minted.json()["key"]
        revoked = client.delete(f"/api/v1/api-keys/{key_id}")
        assert revoked.status_code == 200, revoked.text

    [created] = _events(audit.API_KEY_CREATED)
    assert created.target.type == audit.API_KEY
    assert created.target.id == key_id
    assert created.target.label == "ci"
    assert created.payload["scopes"] == [WORKSPACES_READ]
    [removed] = _events(audit.API_KEY_REVOKED)
    assert removed.payload == {"owner_id": PERSON}
    assert plaintext not in repr(_events())


def test_run_confirm_is_recorded(auth_client, awaiting_confirmation):
    """A confirm names the run against its workspace."""
    run_id = awaiting_confirmation["run_id"]
    response = auth_client.post(f"/api/v1/runs/{run_id}/confirm")
    assert response.status_code == 200, response.text

    [confirmed] = _events(audit.RUN_CONFIRMED)
    assert confirmed.payload == {"run_id": run_id}
    assert confirmed.target.id == awaiting_confirmation["workspace_id"]


def test_a_cli_discard_is_recorded_as_the_cli(auth_client, awaiting_confirmation):
    """A discard through the `tfe.v2` surface reads as coming from the CLI."""
    run_id = awaiting_confirmation["run_id"]
    response = auth_client.post(f"/api/v2/runs/{run_id}/actions/discard", content=b"{}")
    assert response.status_code == 202, response.text

    [discarded] = _events(audit.RUN_DISCARDED)
    assert discarded.payload == {"run_id": run_id}
    assert discarded.actor.source == "cli"


def test_the_listing_is_admin_only(scoped_client):
    """Without `admin` the trail is a 403, whatever else the caller holds."""
    with scoped_client(WORKSPACES_READ, WORKSPACES_WRITE) as client:
        assert client.get(BASE).status_code == 403
        assert client.get(f"{BASE}/export").status_code == 403


def test_the_listing_pages_and_filters(auth_client, workspace):
    """Pages follow the cursor, and the target and action filters narrow the trail."""
    workspace_id = workspace["workspace_id"]
    for key in ("one", "two", "three"):
        response = auth_client.put(
            f"/api/v1/workspaces/{workspace_id}/variables/{key}", json={"value": "x", "category": "env"}
        )
        assert response.status_code == 200, response.text

    first = auth_client.get(BASE, params={"limit": 2})
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]
    assert {"action": audit.VARIABLE_WRITTEN, "label": "Variable written"} in body["event_types"]
    assert all(item["actor_name"] for item in body["items"])
    second = auth_client.get(BASE, params={"limit": 2, "cursor": body["next_cursor"]})
    assert second.status_code == 200, second.text
    listed = [*body["items"], *second.json()["items"]]
    assert sorted(item["action"] for item in listed) == sorted([audit.WORKSPACE_CREATED, *[audit.VARIABLE_WRITTEN] * 3])
    assert len({item["event_id"] for item in listed}) == 4

    narrowed = auth_client.get(
        BASE, params={"target_type": "workspace", "target_id": workspace_id, "action": audit.VARIABLE_WRITTEN}
    )
    assert sorted(item["payload"]["key"] for item in narrowed.json()["items"]) == ["one", "three", "two"]
    elsewhere = auth_client.get(BASE, params={"target_type": "workspace", "target_id": "ws-elsewhere"})
    assert elsewhere.json()["items"] == []


def test_a_bad_filter_or_cursor_is_refused(auth_client, workspace):
    """An unknown action, half a target or a foreign cursor is a 422."""
    assert auth_client.get(BASE, params={"action": "workspace.exploded"}).status_code == 422
    assert auth_client.get(BASE, params={"target_type": "workspace"}).status_code == 422
    assert auth_client.get(BASE, params={"cursor": "not-a-cursor"}).status_code == 422


def test_the_export_is_csv(auth_client, workspace):
    """The export downloads as CSV with a row per event under the shared columns."""
    response = auth_client.get(f"{BASE}/export", params={"action": audit.WORKSPACE_CREATED})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1
    assert rows[0]["action"] == audit.WORKSPACE_CREATED


def test_the_admin_scope_is_what_opens_the_trail(app):
    """A person holding `admin` reads the trail."""
    with _person(app, scopes=(ADMIN,)) as client:
        assert client.get(BASE).status_code == 200
