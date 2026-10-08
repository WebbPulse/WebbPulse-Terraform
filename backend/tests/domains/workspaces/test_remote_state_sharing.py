"""Remote state sharing: a workspace's non-sensitive outputs, read by another workspace's runs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from fastapi.testclient import TestClient
from webbpulse.identity.api_keys import mint

from app.common.core.auth import (
    RUN_API_TOKEN_KIND,
    RUN_TOKEN_TENANT,
    STATE_READ_OUTPUTS,
    WORKSPACES_READ,
    api_key_store,
    effective_run_api_scopes,
)
from app.domains.workspaces.state_versions import state_key
from tests.conftest import REGION, STATE_BUCKET, WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"


@pytest.fixture(autouse=True)
def _versioned_state_bucket(aws_environment) -> None:
    """Turn on object versioning for the state bucket, as the stack does."""
    boto3.client("s3", region_name=REGION).put_bucket_versioning(
        Bucket=STATE_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )


def _create(auth_client, name: str) -> dict:
    """Create a workspace called `name`."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _seed(workspace_id: str, outputs: dict) -> str:
    """Write a state carrying `outputs` the way a run's engine does, returning its version id."""
    body = {"version": 4, "serial": 1, "lineage": "l", "outputs": outputs, "resources": []}
    written = boto3.client("s3", region_name=REGION).put_object(
        Bucket=STATE_BUCKET, Key=state_key(workspace_id), Body=json.dumps(body).encode()
    )
    return written["VersionId"]


def _patch(auth_client, workspace_id: str, body: dict):
    """PATCH a workspace through the admin agent key."""
    return auth_client.patch(f"{BASE}/{workspace_id}", json=body)


def _run_token(auth_client, workspace_id: str, grant: list[str]) -> str:
    """A run API token for a run of `workspace_id`, with that workspace granting `grant`."""
    response = _patch(auth_client, workspace_id, {"run_api_token_scopes": grant})
    assert response.status_code == 200, response.text
    minted = mint(
        user_id="run-01JBQ00000000000000000RUN1",
        tenant_id=RUN_TOKEN_TENANT,
        scopes=effective_run_api_scopes({"run_api_token_scopes": grant}),
        name="api token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        store=api_key_store(),
        kind=RUN_API_TOKEN_KIND,
        metadata={"workspace_id": workspace_id},
    )
    return minted.plaintext


OUTPUTS = {
    "bucket": {"value": "a-bucket", "type": "string"},
    "count": {"value": 3, "type": "number"},
    "password": {"value": "do-not-leak", "type": "string", "sensitive": True},
}


@pytest.fixture
def source(auth_client):
    """A workspace with state holding two plain outputs and one sensitive one."""
    created = _create(auth_client, "source")
    _seed(created["workspace_id"], OUTPUTS)
    return created


@pytest.fixture
def consumer(auth_client):
    """The workspace whose runs read the source's outputs."""
    return _create(auth_client, "consumer")


def _as(app, token: str) -> TestClient:
    """A client presenting `token`."""
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def test_new_workspaces_share_with_no_one(workspace):
    """Sharing is opt in."""
    assert workspace["global_remote_state"] is False
    assert workspace["remote_state_consumer_ids"] == []


def test_an_admin_reads_non_sensitive_outputs_only(auth_client, source):
    """Values for plain outputs, names only for sensitive ones."""
    response = auth_client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["name"] for item in body["outputs"]] == ["bucket", "count"]
    assert body["outputs"][0]["value"] == "a-bucket"
    assert body["outputs"][1]["type"] == "number"
    assert body["sensitive_output_names"] == ["password"]
    assert "do-not-leak" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_a_workspace_without_state_answers_an_empty_list(auth_client, consumer):
    """Nothing applied yet is not an error."""
    response = auth_client.get(f"{BASE}/{consumer['workspace_id']}/outputs")

    assert response.status_code == 200, response.text
    assert response.json()["outputs"] == []
    assert response.json()["state_version_id"] is None


def test_an_unknown_workspace_is_a_404(auth_client):
    """No such source."""
    response = auth_client.get(f"{BASE}/ws-01JBQ000000000000000000000/outputs")

    assert response.status_code == 404


def test_a_read_only_person_key_cannot_read_outputs(scoped_client, source):
    """The scope is never an ordinary grant."""
    with scoped_client(WORKSPACES_READ) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 403


def test_a_run_token_carries_the_scope_beside_workspaces_read():
    """The outputs scope rides on `workspaces:read` and on nothing else."""
    assert STATE_READ_OUTPUTS in effective_run_api_scopes({"run_api_token_scopes": [WORKSPACES_READ]})
    assert STATE_READ_OUTPUTS not in effective_run_api_scopes({"run_api_token_scopes": ["variables:read"]})
    assert effective_run_api_scopes({}) == ()


def test_a_run_is_refused_a_workspace_that_does_not_share(app, auth_client, source, consumer):
    """No sharing means a 403 naming the setting."""
    token = _run_token(auth_client, consumer["workspace_id"], [WORKSPACES_READ])

    with _as(app, token) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 403, response.text
    assert "REMOTE_STATE_NOT_SHARED" in response.text


def test_a_named_consumer_reads_the_outputs(app, auth_client, source, consumer):
    """Listing the consumer's workspace opens the read to its runs."""
    shared = _patch(auth_client, source["workspace_id"], {"remote_state_consumer_ids": [consumer["workspace_id"]]})
    assert shared.status_code == 200, shared.text
    assert shared.json()["remote_state_consumer_ids"] == [consumer["workspace_id"]]
    token = _run_token(auth_client, consumer["workspace_id"], [WORKSPACES_READ])

    with _as(app, token) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["outputs"]] == ["bucket", "count"]
    assert "do-not-leak" not in response.text


def test_global_sharing_opens_the_read_to_every_run(app, auth_client, source, consumer):
    """HCP's "Share with all workspaces"."""
    assert _patch(auth_client, source["workspace_id"], {"global_remote_state": True}).status_code == 200
    token = _run_token(auth_client, consumer["workspace_id"], [WORKSPACES_READ])

    with _as(app, token) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 200, response.text


def test_a_run_reads_its_own_workspace(app, auth_client, source):
    """A workspace always shares with itself."""
    token = _run_token(auth_client, source["workspace_id"], [WORKSPACES_READ])

    with _as(app, token) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 200, response.text


def test_a_run_without_workspaces_read_cannot_read(app, auth_client, source, consumer):
    """A grant without `workspaces:read` carries no outputs scope."""
    _patch(auth_client, source["workspace_id"], {"global_remote_state": True})
    token = _run_token(auth_client, consumer["workspace_id"], ["variables:read"])

    with _as(app, token) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 403


def test_unsharing_takes_effect_at_once(app, auth_client, source, consumer):
    """The allow list is read on every request."""
    _patch(auth_client, source["workspace_id"], {"remote_state_consumer_ids": [consumer["workspace_id"]]})
    token = _run_token(auth_client, consumer["workspace_id"], [WORKSPACES_READ])
    cleared = _patch(auth_client, source["workspace_id"], {"remote_state_consumer_ids": None})
    assert cleared.json()["remote_state_consumer_ids"] == []

    with _as(app, token) as client:
        response = client.get(f"{BASE}/{source['workspace_id']}/outputs")

    assert response.status_code == 403


def test_an_unknown_consumer_is_refused(auth_client, source):
    """Every consumer has to exist."""
    unknown = {"remote_state_consumer_ids": ["ws-01JBQ000000000000000000000"]}

    response = _patch(auth_client, source["workspace_id"], unknown)

    assert response.status_code == 422, response.text
    assert "REMOTE_STATE_CONSUMER_NOT_FOUND" in response.text


def test_a_malformed_consumer_is_refused(auth_client, source):
    """Only workspace ids."""
    response = _patch(auth_client, source["workspace_id"], {"remote_state_consumer_ids": ["not-a-workspace"]})

    assert response.status_code == 422


def test_consumers_are_sorted_deduplicated_and_never_self(auth_client, source, consumer):
    """One canonical form, so resending the list reordered is not a change."""
    other = _create(auth_client, "other")
    ids = [other["workspace_id"], consumer["workspace_id"], other["workspace_id"], source["workspace_id"]]

    response = _patch(auth_client, source["workspace_id"], {"remote_state_consumer_ids": ids})

    assert response.status_code == 200, response.text
    assert response.json()["remote_state_consumer_ids"] == sorted([other["workspace_id"], consumer["workspace_id"]])


def test_turning_global_sharing_off_reads_back_false(auth_client, source):
    """False and null both clear the setting."""
    _patch(auth_client, source["workspace_id"], {"global_remote_state": True})

    response = _patch(auth_client, source["workspace_id"], {"global_remote_state": False})

    assert response.status_code == 200, response.text
    assert response.json()["global_remote_state"] is False


def test_a_non_admin_writer_may_change_sharing(scoped_client, source, consumer):
    """Sharing is a workspace setting, so `workspaces:write` is enough, as on HCP."""
    with scoped_client(WORKSPACES_READ, "workspaces:write") as client:
        response = client.patch(
            f"{BASE}/{source['workspace_id']}", json={"remote_state_consumer_ids": [consumer["workspace_id"]]}
        )

    assert response.status_code == 200, response.text
