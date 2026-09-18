"""The variable routes, and the sensitive value round trip.

The sensitive tests are the point of this file: a sensitive value has to survive
a round trip to the runner and never appear on an API response or in the table.
"""

import boto3

from app.common.db.tables import VARIABLES, local_table_name
from app.domains.workspaces import service as workspaces_service
from tests.conftest import ENVIRONMENT, REGION

BASE = "/api/v1/workspaces"


def variables_url(workspace_id: str, key: str = "") -> str:
    """The variables collection or one variable on it."""
    suffix = f"/{key}" if key else ""
    return f"{BASE}/{workspace_id}/variables{suffix}"


def stored_item(workspace_id: str, key: str) -> dict:
    """The raw table row, read around the service so nothing decrypts it."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(VARIABLES, ENVIRONMENT))
    return table.get_item(Key={"workspace_id": workspace_id, "key": key}).get("Item", {})


def test_put_creates_a_plain_variable(auth_client, workspace):
    """A non sensitive variable is stored and returned with its value."""
    workspace_id = workspace["workspace_id"]
    response = auth_client.put(
        variables_url(workspace_id, "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["value"] == "us-west-2"
    assert body["sensitive"] is False
    assert body["category"] == "terraform"


def test_put_accepts_an_env_variable(auth_client, workspace):
    """Both categories the contract names are accepted."""
    response = auth_client.put(
        variables_url(workspace["workspace_id"], "TF_LOG"),
        json={"value": "DEBUG", "category": "env", "sensitive": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["category"] == "env"


def test_put_rejects_an_unknown_category(auth_client, workspace):
    """A category the runner could not apply is refused."""
    response = auth_client.put(
        variables_url(workspace["workspace_id"], "region"),
        json={"value": "us-west-2", "category": "shell", "sensitive": False},
    )
    assert response.status_code == 422


def test_put_overwrites_and_keeps_created_at(auth_client, workspace):
    """A second put replaces the value and preserves the original `created_at`."""
    workspace_id = workspace["workspace_id"]
    first = auth_client.put(
        variables_url(workspace_id, "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    ).json()
    second = auth_client.put(
        variables_url(workspace_id, "region"),
        json={"value": "us-east-1", "category": "terraform", "sensitive": False},
    ).json()
    assert second["value"] == "us-east-1"
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"]


def test_put_is_404_for_an_absent_workspace(auth_client):
    """A variable cannot be set on a workspace that does not exist."""
    response = auth_client.put(
        variables_url("ws-01JBQ0000000000000000000AA", "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    assert response.status_code == 404


def test_put_rejects_a_key_starting_with_a_digit(auth_client, workspace):
    """The key pattern refuses what neither Terraform nor a shell would accept."""
    response = auth_client.put(
        variables_url(workspace["workspace_id"], "9lives"),
        json={"value": "x", "category": "terraform", "sensitive": False},
    )
    assert response.status_code == 422


def test_sensitive_value_is_not_returned_on_put(auth_client, workspace):
    """The response to setting a sensitive value carries no value."""
    response = auth_client.put(
        variables_url(workspace["workspace_id"], "token"),
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sensitive"] is True
    assert body["value"] is None


def test_sensitive_value_is_not_returned_on_get(auth_client, workspace):
    """Reading a sensitive variable back carries no value either."""
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        variables_url(workspace_id, "token"),
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    body = auth_client.get(variables_url(workspace_id, "token")).json()
    assert body["sensitive"] is True
    assert body["value"] is None


def test_sensitive_value_is_not_returned_in_a_list(auth_client, workspace):
    """A list never leaks a sensitive value, including alongside plain ones."""
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        variables_url(workspace_id, "token"),
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    auth_client.put(
        variables_url(workspace_id, "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    items = {item["key"]: item for item in auth_client.get(variables_url(workspace_id)).json()["items"]}
    assert items["token"]["value"] is None
    assert items["region"]["value"] == "us-west-2"


def test_sensitive_value_is_not_stored_in_the_clear(auth_client, workspace):
    """The plaintext is absent from the row, and the envelope fields are present."""
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        variables_url(workspace_id, "token"),
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    item = stored_item(workspace_id, "token")
    assert "value" not in item
    assert "super-secret" not in str(item)
    assert item["secret_scheme"] == "secret-hkdf-v1"
    assert item["secret_ciphertext"]
    assert item["secret_nonce"]


def test_sensitive_value_round_trips_for_the_runner(auth_client, workspace):
    """The decrypted value reaches the bundle path, which is the only reader.

    The round trip is the whole point of sealing app side: the API never returns
    the value, and `resolved_variables` is the one function that opens it.
    """
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        variables_url(workspace_id, "token"),
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    auth_client.put(
        variables_url(workspace_id, "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    resolved = workspaces_service.resolved_variables(workspace_id)
    assert resolved["env"]["token"] == "super-secret"
    assert resolved["terraform"]["region"] == "us-west-2"


def test_a_sealed_value_cannot_be_moved_to_another_key(auth_client, workspace):
    """The encryption context binds a ciphertext to one workspace and key.

    Copying the envelope onto a different key makes it undecryptable, so a write
    to the table cannot relocate a secret to somewhere it would be read out.
    """
    import pytest
    from webbpulse.identity.crypto import EnvelopeDecryptionFailed

    workspace_id = workspace["workspace_id"]
    auth_client.put(
        variables_url(workspace_id, "token"),
        json={"value": "super-secret", "category": "env", "sensitive": True},
    )
    sealed = stored_item(workspace_id, "token")
    moved = {field: sealed[field] for field in sealed if field != "key"}
    moved["key"] = "other"
    boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(VARIABLES, ENVIRONMENT)).put_item(Item=moved)

    with pytest.raises(EnvelopeDecryptionFailed):
        workspaces_service.resolved_variables(workspace_id)


def test_get_is_404_for_an_absent_variable(auth_client, workspace):
    """A key nobody set is a 404."""
    response = auth_client.get(variables_url(workspace["workspace_id"], "missing"))
    assert response.status_code == 404


def test_delete_removes_the_variable(auth_client, workspace):
    """A delete returns 204 and the variable stops reading back."""
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        variables_url(workspace_id, "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    assert auth_client.delete(variables_url(workspace_id, "region")).status_code == 204
    assert auth_client.get(variables_url(workspace_id, "region")).status_code == 404


def test_delete_is_404_for_an_absent_variable(auth_client, workspace):
    """A delete of nothing is a 404."""
    response = auth_client.delete(variables_url(workspace["workspace_id"], "missing"))
    assert response.status_code == 404


def test_list_is_empty_for_a_new_workspace(auth_client, workspace):
    """A workspace starts with no variables."""
    response = auth_client.get(variables_url(workspace["workspace_id"]))
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_variables_are_scoped_to_their_workspace(auth_client, workspace):
    """One workspace's variables are invisible from another."""
    other = auth_client.post(
        BASE,
        json={
            "name": "other",
            "engine": "terraform",
            "engine_version": "1.11.4",
            "run_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run",
        },
    ).json()
    auth_client.put(
        variables_url(workspace["workspace_id"], "region"),
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    assert auth_client.get(variables_url(other["workspace_id"])).json()["items"] == []
