"""Who triggered a run, recorded at create and never invented afterwards."""

import boto3
from fastapi.testclient import TestClient
from webbpulse.identity.claims import AuthorizerClaims

from app.common.core.auth import ALL_SCOPES, claims
from app.common.db.tables import RUNS, SEMAPHORE_RUN_ID, local_table_name
from app.domains.runs import service as runs_service
from app.domains.runs.actor import actor_from_claims
from tests.conftest import ENVIRONMENT, REGION, mint_key

BASE = "/api/v1/runs"


def runs_table():
    """The local runs table, read around the service."""
    return boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))


def strip_actor(run_id: str) -> None:
    """Turn a run into a legacy row written before attribution shipped."""
    runs_table().update_item(Key={"run_id": run_id}, UpdateExpression="REMOVE actor")


def test_an_api_key_run_is_attributed_to_its_minter(created_run):
    """A run created through a `wpk_` key records the `agent` kind and the minting user."""
    assert created_run["actor"] == {"kind": "agent", "id": "user-test", "display_name": None}


def test_the_actor_is_stored_on_the_row(created_run):
    """The actor is written with the run, so it survives a read by id."""
    item = runs_table().get_item(Key={"run_id": created_run["run_id"]}).get("Item", {})
    assert item["actor"] == {"kind": "agent", "id": "user-test"}


def test_the_actor_survives_a_read_back(auth_client, created_run):
    """Reading the run by id renders the same actor the create returned."""
    fetched = auth_client.get(f"{BASE}/{created_run['run_id']}").json()
    assert fetched["actor"] == created_run["actor"]


def test_a_user_credential_records_the_user_kind():
    """Claims with a subject and no API key marker are a person, with their name."""
    actor = actor_from_claims({"sub": "user-42", "display_name": "Ada Lovelace"})
    assert actor == {"kind": "user", "id": "user-42", "display_name": "Ada Lovelace"}


def test_a_user_without_a_display_name_carries_only_the_id():
    """No name claim means no name, rather than a made-up one."""
    assert actor_from_claims({"sub": "user-42"}) == {"kind": "user", "id": "user-42"}


def test_an_email_stands_in_for_an_absent_display_name():
    """An address is the last-resort label."""
    assert actor_from_claims({"sub": "user-42", "email": "ada@example.test"})["display_name"] == "ada@example.test"


def test_claims_naming_no_subject_record_no_actor():
    """Claims that name nobody are unknown, not attributed to anyone."""
    assert actor_from_claims(None) is None
    assert actor_from_claims({}) is None
    assert actor_from_claims({"sub": "  ", "display_name": "Ghost"}) is None


def test_a_run_with_no_actor_stores_none(workspace, uploaded_config_version, state_machine):
    """The service stores no actor rather than a placeholder when given none."""
    created = runs_service.create_run(
        {
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": True,
        },
        actor=None,
    )
    assert "actor" not in runs_table().get_item(Key={"run_id": created["run_id"]}).get("Item", {})


def test_a_legacy_row_serves_the_api_with_a_null_actor(auth_client, created_run):
    """A run written before attribution shipped renders `actor: null` by id and in both lists."""
    run_id = created_run["run_id"]
    strip_actor(run_id)

    assert auth_client.get(f"{BASE}/{run_id}").json()["actor"] is None
    scoped = auth_client.get(BASE, params={"workspace_id": created_run["workspace_id"]}).json()["items"]
    assert [(run["run_id"], run["actor"]) for run in scoped] == [(run_id, None)]
    unscoped = auth_client.get(BASE).json()["items"]
    assert [(run["run_id"], run["actor"]) for run in unscoped] == [(run_id, None)]


def test_two_keys_record_two_different_actors(app, workspace, uploaded_config_version, state_machine):
    """Each run names the principal that created it, not whichever came last."""
    body = {
        "workspace_id": workspace["workspace_id"],
        "config_version_id": uploaded_config_version["config_version_id"],
        "plan_only": True,
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
    """The reserved row is not a run, so it is neither attributed nor indexed."""
    item = runs_table().get_item(Key={"run_id": SEMAPHORE_RUN_ID}).get("Item", {})
    assert "actor" not in item
    assert "collection" not in item


def test_a_queued_run_keeps_its_creator_on_promotion(created_run, uploaded_config_version):
    """Starting a queued run preserves the actor recorded when it was created."""
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
    assert promoted["actor"] == actor


def test_the_route_uses_verified_claims_not_the_payload(
    app, auth_client, workspace, uploaded_config_version, state_machine
):
    """An `actor` in the request body cannot replace the principal the guard verified."""

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
            },
        )
    finally:
        app.dependency_overrides.pop(claims)
    assert response.status_code == 201, response.text
    assert response.json()["actor"] == {"kind": "user", "id": "real-user", "display_name": "Real User"}
