"""The workspace routes: create, read, list, update, delete.

The update cases carry the JSON Merge Patch contract: an omitted key leaves the
stored value alone, an explicit null clears a clearable field, and a clear removes
the attribute rather than writing a null into the row.
"""

from app.domains.workspaces import service
from tests.conftest import WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"

OTHER_ROLE_ARN = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-workspace-second"
"""A second valid ARN, so a change is distinguishable from the created one."""


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


def test_patch_sets_a_run_role_arn_on_a_workspace_without_one(auth_client):
    """A workspace created with no role takes one through a later PATCH."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    created = auth_client.post(BASE, json=payload).json()
    assert created["run_role_arn"] is None

    response = auth_client.patch(
        f"{BASE}/{created['workspace_id']}",
        json={"run_role_arn": OTHER_ROLE_ARN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] == OTHER_ROLE_ARN


def test_patch_changes_an_existing_run_role_arn(auth_client, workspace):
    """A second PATCH replaces the stored ARN rather than appending to it."""
    response = auth_client.patch(
        f"{BASE}/{workspace['workspace_id']}",
        json={"run_role_arn": OTHER_ROLE_ARN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] == OTHER_ROLE_ARN


def test_patch_clears_the_run_role_arn_with_an_explicit_null(auth_client, workspace):
    """An explicit null unsets the ARN, which the old None filter made impossible."""
    workspace_id = workspace["workspace_id"]
    assert workspace["run_role_arn"]

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": None})
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] is None
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["run_role_arn"] is None


def test_clearing_the_run_role_arn_removes_the_attribute_rather_than_storing_null(auth_client, workspace):
    """The row loses the attribute, so nothing downstream reads a stored null."""
    workspace_id = workspace["workspace_id"]
    auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": None})
    assert "run_role_arn" not in service.get_workspace(workspace_id)


def test_clearing_the_run_role_arn_makes_the_check_a_400(auth_client, workspace):
    """A cleared role leaves the workspace in the same state as one never set."""
    workspace_id = workspace["workspace_id"]
    auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": None})
    response = auth_client.post(f"{BASE}/{workspace_id}/run-role/check")
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "RUN_ROLE_MISSING"


def test_clearing_the_run_role_arn_drops_the_recorded_check(auth_client, workspace):
    """The previous success belonged to the previous role, so it goes with it."""
    workspace_id = workspace["workspace_id"]
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["run_role_checked_at"]

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": None})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_role_checked_at"] is None
    assert body["run_role_account_id"] is None


def test_patch_clears_the_description_with_an_explicit_null(auth_client, workspace):
    """A cleared description reads back as the empty string, not as null."""
    workspace_id = workspace["workspace_id"]
    assert workspace["description"] == "An example workspace."

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"description": None})
    assert response.status_code == 200, response.text
    assert response.json()["description"] == ""
    assert "description" not in service.get_workspace(workspace_id)


def test_patch_clears_the_working_directory_with_an_explicit_null(auth_client, workspace):
    """The third clearable field behaves the same way as the other two."""
    workspace_id = workspace["workspace_id"]
    auth_client.patch(f"{BASE}/{workspace_id}", json={"working_directory": "modules/net"})
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["working_directory"] == "modules/net"

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"working_directory": None})
    assert response.status_code == 200, response.text
    assert response.json()["working_directory"] == ""
    assert "working_directory" not in service.get_workspace(workspace_id)


def test_patch_leaves_an_omitted_key_completely_untouched(auth_client, workspace):
    """The regression this change risks: an omitted key must not be cleared.

    Every field but the one named is compared against the created row, so a PATCH
    that widens its write beyond its body fails here rather than in production.
    """
    workspace_id = workspace["workspace_id"]
    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"engine_version": "1.12.0"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["engine_version"] == "1.12.0"
    for field in ("run_role_arn", "description", "working_directory", "engine", "name", "created_at"):
        assert body[field] == workspace[field], field


def test_patch_with_an_empty_body_changes_nothing(auth_client, workspace):
    """No key at all is not a clear of every clearable field."""
    workspace_id = workspace["workspace_id"]
    response = auth_client.patch(f"{BASE}/{workspace_id}", json={})
    assert response.status_code == 200, response.text
    assert response.json() == workspace


def test_clearing_an_already_absent_field_is_a_no_op(auth_client):
    """A null on a field that was never set succeeds and stores no null."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    created = auth_client.post(BASE, json=payload).json()
    workspace_id = created["workspace_id"]

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": None})
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] is None
    assert "run_role_arn" not in service.get_workspace(workspace_id)


def test_patch_sets_and_clears_in_one_body(auth_client, workspace):
    """One request may assign one field and clear another."""
    workspace_id = workspace["workspace_id"]
    response = auth_client.patch(
        f"{BASE}/{workspace_id}",
        json={"engine_version": "1.13.0", "description": None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine_version"] == "1.13.0"
    assert body["description"] == ""


def test_patch_refuses_a_null_engine_version(auth_client, workspace):
    """A stored workspace has to carry an engine version, so a null is a 422."""
    response = auth_client.patch(
        f"{BASE}/{workspace['workspace_id']}",
        json={"engine_version": None},
    )
    assert response.status_code == 422


def test_patch_refuses_a_null_engine(auth_client, workspace):
    """The engine is not clearable either, and fails loudly rather than silently."""
    response = auth_client.patch(f"{BASE}/{workspace['workspace_id']}", json={"engine": None})
    assert response.status_code == 422
