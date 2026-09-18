"""The workspace routes: create, read, list, update, delete."""

from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"


def test_create_returns_201_and_an_id(auth_client):
    """A create returns the stored workspace with a `ws-` prefixed ULID."""
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace_id"].startswith("ws-")
    assert body["name"] == "example"
    assert body["engine"] == "terraform"
    assert body["created_at"]


def test_create_defaults_the_engine_to_terraform(auth_client):
    """Omitting the engine picks terraform rather than failing validation."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "engine"}
    response = auth_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["engine"] == "terraform"


def test_create_accepts_tofu(auth_client):
    """Both engines the runner image bundles are accepted."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "engine": "tofu"})
    assert response.status_code == 201, response.text
    assert response.json()["engine"] == "tofu"


def test_create_rejects_an_unknown_engine(auth_client):
    """An engine the runner cannot run is refused at validation."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "engine": "pulumi"})
    assert response.status_code == 422


def test_create_rejects_a_duplicate_name(auth_client, workspace):
    """The `by_name` uniqueness claim renders as a 409, not a second row."""
    response = auth_client.post(BASE, json=WORKSPACE_PAYLOAD)
    assert response.status_code == 409, response.text


def test_create_allows_a_second_distinct_name(auth_client, workspace):
    """Uniqueness is per name, not a single workspace limit."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "name": "another"})
    assert response.status_code == 201, response.text


def test_create_rejects_a_name_starting_with_punctuation(auth_client):
    """The name pattern refuses a leading dot, which would read as a path."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "name": ".hidden"})
    assert response.status_code == 422


def test_get_returns_the_workspace(auth_client, workspace):
    """A workspace reads back by id."""
    response = auth_client.get(f"{BASE}/{workspace['workspace_id']}")
    assert response.status_code == 200, response.text
    assert response.json() == workspace


def test_get_is_404_for_an_absent_workspace(auth_client):
    """A well formed id that names nothing is a 404."""
    response = auth_client.get(f"{BASE}/ws-01JBQ0000000000000000000AA")
    assert response.status_code == 404


def test_get_is_422_for_a_malformed_id(auth_client):
    """An id that is not a `ws-` ULID never reaches the table."""
    response = auth_client.get(f"{BASE}/not-an-id")
    assert response.status_code == 422


def test_list_returns_every_workspace(auth_client, workspace):
    """The list carries the created workspace."""
    auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "name": "second"})
    response = auth_client.get(BASE)
    assert response.status_code == 200, response.text
    names = sorted(item["name"] for item in response.json()["items"])
    assert names == ["example", "second"]


def test_list_is_empty_before_anything_is_created(auth_client):
    """An empty estate lists nothing rather than failing."""
    response = auth_client.get(BASE)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_patch_updates_the_engine_version(auth_client, workspace):
    """A partial edit changes the named field and stamps `updated_at`."""
    response = auth_client.patch(
        f"{BASE}/{workspace['workspace_id']}",
        json={"engine_version": "1.12.0"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine_version"] == "1.12.0"
    assert body["updated_at"]
    assert body["name"] == workspace["name"]


def test_patch_ignores_a_name(auth_client, workspace):
    """A rename is refused by omission: the name is not an editable field.

    A rename would break the state key and the uniqueness claim at once, so the
    field is absent from the update model and a caller sending one is ignored
    rather than served a partial rename.
    """
    response = auth_client.patch(
        f"{BASE}/{workspace['workspace_id']}",
        json={"name": "renamed"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "example"


def test_patch_is_404_for_an_absent_workspace(auth_client):
    """An update against nothing is a 404 rather than an upsert."""
    response = auth_client.patch(
        f"{BASE}/ws-01JBQ0000000000000000000AA",
        json={"engine_version": "1.12.0"},
    )
    assert response.status_code == 404


def test_delete_removes_the_workspace(auth_client, workspace):
    """A delete returns 204 and the workspace stops reading back."""
    workspace_id = workspace["workspace_id"]
    assert auth_client.delete(f"{BASE}/{workspace_id}").status_code == 204
    assert auth_client.get(f"{BASE}/{workspace_id}").status_code == 404


def test_delete_frees_the_name(auth_client, workspace):
    """A deleted workspace's name can be claimed again."""
    auth_client.delete(f"{BASE}/{workspace['workspace_id']}")
    assert auth_client.post(BASE, json=WORKSPACE_PAYLOAD).status_code == 201


def test_delete_also_removes_the_variables(auth_client, workspace):
    """Deleting a workspace deletes its variables rather than orphaning them."""
    workspace_id = workspace["workspace_id"]
    auth_client.put(
        f"{BASE}/{workspace_id}/variables/region",
        json={"value": "us-west-2", "category": "terraform", "sensitive": False},
    )
    auth_client.delete(f"{BASE}/{workspace_id}")
    auth_client.post(BASE, json=WORKSPACE_PAYLOAD)
    recreated = auth_client.get(BASE).json()["items"][0]["workspace_id"]
    assert auth_client.get(f"{BASE}/{recreated}/variables").json()["items"] == []


def test_delete_is_404_for_an_absent_workspace(auth_client):
    """A delete of nothing is a 404, so a caller learns it deleted nothing."""
    response = auth_client.delete(f"{BASE}/ws-01JBQ0000000000000000000AA")
    assert response.status_code == 404
