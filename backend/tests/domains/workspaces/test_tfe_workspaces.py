"""The `tfe.v2` reads a `cloud {}` block makes during `terraform init`.

The shapes are pinned field by field where go-tfe decodes them, because a wrong
resource `type` or a missing relationship fails the CLI with a decode error rather
than anything a person could act on.
"""

from __future__ import annotations

from app.common.core.auth import (
    RUNS_APPLY,
    RUNS_READ,
    RUNS_WRITE,
    VARIABLES_READ,
    WORKSPACES_READ,
)
from tests.conftest import WORKSPACE_PAYLOAD

API = "/api/v2"
ORG = f"{API}/organizations/WebbPulse"
JSON_API = "application/vnd.api+json"


def test_ping_answers_the_api_version_without_credentials(client):
    """go-tfe reads `TFP-API-Version` off ping before it sends anything else."""
    response = client.get(f"{API}/ping")
    assert response.status_code == 204
    assert response.headers["TFP-API-Version"] == "2.6"
    assert "TFP-AppName" not in response.headers


def test_entitlements_keep_operations_remote(auth_client):
    """`operations` true is what stops the cloud backend running plans locally."""
    response = auth_client.get(f"{ORG}/entitlement-set")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(JSON_API)
    data = response.json()["data"]
    assert data["type"] == "entitlement-sets"
    assert data["attributes"]["operations"] is True
    assert data["attributes"]["state-storage"] is True


def test_an_unknown_organization_is_a_json_api_404(auth_client):
    """Any organization but WebbPulse is not found, in the errors shape go-tfe reads."""
    response = auth_client.get(f"{API}/organizations/hashicorp/entitlement-set")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(JSON_API)
    error = response.json()["errors"][0]
    assert error["status"] == "404"
    assert error["title"] == "not found"


def test_no_credential_is_a_json_api_401(client):
    """An unauthenticated read is a 401 with the bearer challenge kept."""
    response = client.get(f"{ORG}/entitlement-set")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["errors"][0]["status"] == "401"


def test_a_key_without_workspaces_read_is_a_json_api_403(scoped_client):
    """The scope check's refusal reaches the CLI as a JSON:API error, too."""
    response = scoped_client(RUNS_READ).get(f"{ORG}/entitlement-set")
    assert response.status_code == 403
    assert response.json()["errors"][0]["status"] == "403"


def test_workspace_by_name_renders_the_hcp_resource(auth_client, workspace):
    """The read the cloud backend's StateMgr makes, with every relation go-tfe types."""
    response = auth_client.get(f"{ORG}/workspaces/{WORKSPACE_PAYLOAD['name']}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["type"] == "workspaces"
    assert data["id"] == workspace["workspace_id"]
    attributes = data["attributes"]
    assert attributes["name"] == "example"
    assert attributes["terraform-version"] == "1.11.4"
    assert attributes["execution-mode"] == "remote"
    assert attributes["operations"] is True
    assert attributes["structured-run-output-enabled"] is False
    assert attributes["vcs-repo"] is None
    assert attributes["locked"] is False
    assert attributes["created-at"].endswith("Z")
    assert "can-force-delete" not in attributes["permissions"]
    relationships = data["relationships"]
    assert relationships["organization"] == {"data": {"type": "organizations", "id": "WebbPulse"}}
    assert relationships["current-run"] == {"data": None}
    assert relationships["current-state-version"] == {"data": None}


def test_permissions_follow_the_callers_scopes(scoped_client, workspace):
    """A read only key may not queue runs; a key with runs:apply may queue applies."""
    name = WORKSPACE_PAYLOAD["name"]
    reader = scoped_client(WORKSPACES_READ).get(f"{ORG}/workspaces/{name}").json()["data"]["attributes"]
    assert reader["permissions"]["can-read-settings"] is True
    assert reader["permissions"]["can-queue-run"] is False
    assert reader["permissions"]["can-queue-apply"] is False
    assert reader["permissions"]["can-update"] is False

    runner = scoped_client(WORKSPACES_READ, RUNS_WRITE, RUNS_APPLY).get(f"{ORG}/workspaces/{name}")
    permissions = runner.json()["data"]["attributes"]["permissions"]
    assert permissions["can-queue-run"] is True
    assert permissions["can-queue-apply"] is True
    assert permissions["can-lock"] is False


def test_an_unknown_workspace_name_is_404(auth_client):
    """A 404 is what makes the cloud backend try to create the workspace."""
    response = auth_client.get(f"{ORG}/workspaces/missing")
    assert response.status_code == 404
    assert response.json()["errors"][0]["status"] == "404"


def test_creating_a_workspace_from_the_cli_is_refused_with_a_reason(auth_client):
    """The CLI shows the detail, which says where workspaces are created instead."""
    response = auth_client.post(
        f"{ORG}/workspaces",
        json={"data": {"type": "workspaces", "attributes": {"name": "new"}}},
        headers={"content-type": JSON_API},
    )
    assert response.status_code == 422
    assert "UI" in response.json()["errors"][0]["detail"]


def test_workspace_list_filters_and_paginates(auth_client, workspace):
    """`search[name]` narrows the list and `meta.pagination` is what go-tfe pages by."""
    second = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"name": "second"})
    assert second.status_code == 201, second.text

    everything = auth_client.get(f"{ORG}/workspaces").json()
    assert [item["attributes"]["name"] for item in everything["data"]] == ["example", "second"]
    assert everything["meta"]["pagination"]["total-count"] == 2

    paged = auth_client.get(f"{ORG}/workspaces", params={"page[size]": 1, "page[number]": 2}).json()
    assert [item["attributes"]["name"] for item in paged["data"]] == ["second"]
    assert paged["meta"]["pagination"]["prev-page"] == 1
    assert paged["meta"]["pagination"]["next-page"] is None
    assert paged["meta"]["pagination"]["total-pages"] == 2

    searched = auth_client.get(f"{ORG}/workspaces", params={"search[name]": "sec"}).json()
    assert [item["attributes"]["name"] for item in searched["data"]] == ["second"]

    tagged = auth_client.get(f"{ORG}/workspaces", params={"search[tags]": "app"}).json()
    assert tagged["data"] == []


def test_workspace_by_id(auth_client, workspace):
    """The read go-tfe's ReadByID makes, in the same shape as the read by name."""
    workspace_id = workspace["workspace_id"]
    response = auth_client.get(f"{API}/workspaces/{workspace_id}")
    assert response.status_code == 200
    assert response.json()["data"]["attributes"]["name"] == "example"
    missing = auth_client.get(f"{API}/workspaces/ws-01JBQ0000000000000000000AA")
    assert missing.status_code == 404


def test_all_vars_withholds_sensitive_values(auth_client, scoped_client, workspace):
    """Local operations read every variable, a sensitive one with a null value."""
    workspace_id = workspace["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}/variables"
    auth_client.put(f"{base}/region", json={"value": "us-west-2", "category": "terraform", "sensitive": False})
    auth_client.put(f"{base}/secret", json={"value": "hidden", "category": "env", "sensitive": True})

    response = scoped_client(WORKSPACES_READ, VARIABLES_READ).get(f"{API}/workspaces/{workspace_id}/all-vars")
    assert response.status_code == 200
    variables = {item["attributes"]["key"]: item for item in response.json()["data"]}
    assert variables["region"]["type"] == "vars"
    assert variables["region"]["attributes"]["value"] == "us-west-2"
    assert variables["region"]["attributes"]["category"] == "terraform"
    assert variables["secret"]["attributes"]["value"] is None
    assert variables["secret"]["attributes"]["sensitive"] is True
    assert variables["region"]["relationships"]["configurable"]["data"] == {"type": "workspaces", "id": workspace_id}
    assert variables["region"]["id"] != variables["secret"]["id"]

    refused = scoped_client(WORKSPACES_READ).get(f"{API}/workspaces/{workspace_id}/all-vars")
    assert refused.status_code == 403
