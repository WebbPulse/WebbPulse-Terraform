"""Who triggered a run, and what a run with no principal behind it records.

The attribution is unrecoverable after the fact, so these cover the three ways a
run can be created and the one way a row can arrive without an actor at all.
"""

import boto3
from webbpulse.identity.claims import AuthorizerClaims

from app.common.core.auth import ALL_SCOPES, claims
from app.common.db.tables import RUNS, local_table_name
from app.domains.runs import service as runs_service
from app.domains.runs.actor import actor_from_claims, actor_from_row
from tests.conftest import ENVIRONMENT, REGION, mint_key

BASE = "/api/v1/runs"


def stored_run(run_id: str) -> dict:
    """The raw run row, read around the service so the flat columns are visible."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    return table.get_item(Key={"run_id": run_id}).get("Item", {})


def test_an_api_key_run_is_attributed_to_its_minter(auth_client, created_run):
    """A run created through a `wpk_` key names the minting user and the `agent` kind.

    The suite authenticates with a key, and the package marks a key's claims with
    `actor_kind`, so this is the agent branch rather than the user one.
    """
    actor = created_run["actor"]
    assert actor["kind"] == "agent"
    assert actor["id"] == "user-test"


def test_the_actor_is_stored_as_flat_columns(created_run):
    """The row carries `actor_kind` and `actor_id` top level, not a nested map.

    Flat because `by_recency` projects individual attributes and DynamoDB cannot
    project into a nested document, so a nested actor would not survive the index.
    """
    item = stored_run(created_run["run_id"])
    assert item["actor_kind"] == "agent"
    assert item["actor_id"] == "user-test"
    assert "actor" not in item


def test_the_actor_survives_a_read_back(auth_client, created_run):
    """Reading the run by id renders the same actor the create returned."""
    fetched = auth_client.get(f"{BASE}/{created_run['run_id']}").json()
    assert fetched["actor"] == created_run["actor"]


def test_a_user_credential_records_the_user_kind():
    """Claims with a subject and no API key marker are a person, with their name."""
    actor = actor_from_claims({"sub": "user-42", "display_name": "Ada Lovelace"})
    assert actor == {"kind": "user", "id": "user-42", "display_name": "Ada Lovelace"}


def test_a_user_without_a_display_name_carries_only_the_id():
    """An actor that cannot name a principal leaves the name out rather than faking one."""
    assert actor_from_claims({"sub": "user-42"}) == {"kind": "user", "id": "user-42"}


def test_an_email_stands_in_for_an_absent_display_name():
    """An address is a poor label and a better one than a bare uuid."""
    actor = actor_from_claims({"sub": "user-42", "email": "ada@example.test"})
    assert actor["display_name"] == "ada@example.test"


def test_no_principal_is_a_system_actor():
    """An internal path with no claims records `system` rather than borrowing a person."""
    assert actor_from_claims(None) == {"kind": "system"}
    assert actor_from_claims({}) == {"kind": "system"}


def test_a_subjectless_credential_is_a_system_actor():
    """Claims that authenticate nobody name nobody, and the create still succeeds."""
    assert actor_from_claims({"sub": "  ", "display_name": "Ghost"}) == {"kind": "system"}


def test_a_run_created_without_a_principal_stores_system(
    workspace,
    uploaded_config_version,
    state_machine,
):
    """The service default is `system`, so an internal create cannot crash on attribution."""
    created = runs_service.create_run(
        {
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": True,
            "message": "",
        }
    )

    assert runs_service.render_run(created)["actor"] == {"kind": "system"}
    assert stored_run(created["run_id"])["actor_kind"] == "system"


def test_a_legacy_row_with_no_actor_reads_as_none(created_run):
    """A run written before attribution shipped renders `actor` as null, not an error.

    Nothing backfills: the principal was never recorded, and inventing one would be
    a lie that reads like an audit trail.
    """
    run_id = created_run["run_id"]
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    table.update_item(
        Key={"run_id": run_id},
        UpdateExpression="REMOVE actor_kind, actor_id, actor_display_name",
    )

    assert runs_service.render_run(runs_service.get_run(run_id))["actor"] is None


def test_a_legacy_row_serves_the_api_without_an_actor(auth_client, created_run):
    """The route renders a legacy row rather than failing its response model."""
    run_id = created_run["run_id"]
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    table.update_item(
        Key={"run_id": run_id},
        UpdateExpression="REMOVE actor_kind, actor_id, actor_display_name",
    )

    response = auth_client.get(f"{BASE}/{run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["actor"] is None


def test_actor_from_row_ignores_blank_columns():
    """A stored empty string is treated as absent, the way it is never written."""
    assert actor_from_row({"actor_kind": "user", "actor_id": "u1", "actor_display_name": "  "}) == {
        "kind": "user",
        "id": "u1",
    }


def test_two_keys_record_two_different_actors(
    app,
    workspace,
    uploaded_config_version,
    state_machine,
):
    """Each run names the principal that created it, not whichever came last."""
    from fastapi.testclient import TestClient

    workspace_id = workspace["workspace_id"]
    config_version_id = uploaded_config_version["config_version_id"]
    body = {
        "workspace_id": workspace_id,
        "config_version_id": config_version_id,
        "plan_only": True,
        "message": "",
    }

    created: list[dict] = []
    for user_id in ("user-one", "user-two"):
        token = mint_key(*ALL_SCOPES, user_id=user_id)
        with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
            response = client.post(BASE, json=body)
            assert response.status_code == 201, response.text
            created.append(response.json())

    assert [run["actor"]["id"] for run in created] == ["user-one", "user-two"]


def test_the_semaphore_row_carries_no_actor(created_run):
    """The reserved row is not a run, so nothing stamps an actor onto it."""
    item = stored_run("run-semaphore")
    assert "actor_kind" not in item
    assert "collection" not in item


def test_queued_run_keeps_its_creator_on_promotion(created_run, uploaded_config_version):
    """Starting a queued run must preserve the original principal's attribution."""
    actor = {"kind": "user", "id": "queue-owner", "display_name": "Queue Owner"}
    queued = runs_service.create_run(
        {
            "workspace_id": created_run["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
        },
        actor=actor,
    )
    assert queued["status"] == "pending"
    runs_service.finish_run(created_run["run_id"], status="cancelled")
    promoted = runs_service.get_run(queued["run_id"])
    assert promoted["status"] == "planning"
    assert runs_service.render_run(promoted)["actor"] == actor


def test_user_route_uses_verified_claims_not_payload(
    app, auth_client, workspace, uploaded_config_version, state_machine
):
    """Payload attribution cannot replace the principal resolved by the auth guard."""

    def verified_user():
        """Stand in for claims returned by the verified JWT dependency."""
        return AuthorizerClaims({"sub": "real-user", "display_name": "Real User", "scope": "runs:write"})

    app.dependency_overrides[claims] = verified_user
    try:
        response = auth_client.post(
            BASE,
            json={
                "workspace_id": workspace["workspace_id"],
                "config_version_id": uploaded_config_version["config_version_id"],
                "actor": {"kind": "user", "id": "forged-user"},
                "actor_kind": "system",
            },
        )
    finally:
        app.dependency_overrides.pop(claims)
    assert response.status_code == 201
    assert response.json()["actor"] == {"kind": "user", "id": "real-user", "display_name": "Real User"}


def test_run_token_cannot_create_or_list_runs(runner_client, created_run):
    """Runner credentials cannot masquerade as an agent on the public run routes."""
    assert runner_client.get(BASE).status_code == 403
    assert runner_client.get(BASE, params={"workspace_id": created_run["workspace_id"]}).status_code == 403
    assert (
        runner_client.post(
            BASE,
            json={"workspace_id": created_run["workspace_id"], "config_version_id": created_run["config_version_id"]},
        ).status_code
        == 403
    )
