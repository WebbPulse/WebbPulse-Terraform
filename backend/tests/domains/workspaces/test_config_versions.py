"""The config version routes and the presigned upload they hand back."""

from app.domains.workspaces import service as workspaces_service

BASE = "/api/v1/workspaces"


def configs_url(workspace_id: str, config_version_id: str = "") -> str:
    """The config versions collection or one version on it."""
    suffix = f"/{config_version_id}" if config_version_id else ""
    return f"{BASE}/{workspace_id}/config-versions{suffix}"


def test_post_returns_a_pending_version_and_an_upload(auth_client, workspace):
    """A create returns the row plus a presigned PUT and its signed headers."""
    response = auth_client.post(configs_url(workspace["workspace_id"]), json={"size_bytes": 2048})
    assert response.status_code == 201, response.text
    body = response.json()
    version = body["config_version"]
    assert version["config_version_id"].startswith("cv-")
    assert version["status"] == "pending"
    assert version["size_bytes"] == 2048
    assert body["upload_url"].startswith("https://")
    assert body["headers"]["Content-Type"] == "application/gzip"
    assert body["expires_in"] == 900


def test_the_key_follows_the_contract_layout(auth_client, workspace):
    """The object key is the layout the runner's presigned GET is built from."""
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(configs_url(workspace_id), json={"size_bytes": 1024}).json()["config_version"]
    expected = f"configs/{workspace_id}/{version['config_version_id']}.tar.gz"
    assert version["key"] == expected


def test_the_presigned_put_signs_the_type_and_the_length(auth_client, workspace):
    """The content type and the length ceiling are inside the signature.

    Asserted on the signed URL rather than by PUTting to moto, whose
    virtual-host endpoint answers from us-east-1 regardless of the bucket's
    region and so rejects a correctly signed request for the wrong reason.
    """
    from urllib.parse import parse_qs, urlparse

    body = auth_client.post(configs_url(workspace["workspace_id"]), json={"size_bytes": 11}).json()
    query = parse_qs(urlparse(body["upload_url"]).query)
    signed = query["X-Amz-SignedHeaders"][0]
    assert "content-type" in signed
    assert "content-length" in signed
    assert body["headers"]["Content-Type"] == "application/gzip"
    assert body["headers"]["Content-Length"] == "11"


def test_post_rejects_a_size_over_the_ceiling(auth_client, workspace):
    """A size past the ceiling is refused before anything is signed."""
    response = auth_client.post(
        configs_url(workspace["workspace_id"]),
        json={"size_bytes": 250_000_001},
    )
    assert response.status_code == 422


def test_post_rejects_a_zero_size(auth_client, workspace):
    """A zero byte ceiling would bound nothing, so it is refused."""
    response = auth_client.post(configs_url(workspace["workspace_id"]), json={"size_bytes": 0})
    assert response.status_code == 422


def test_post_is_404_for_an_absent_workspace(auth_client):
    """A config version cannot be created against a workspace that is not there."""
    response = auth_client.post(
        configs_url("ws-01JBQ0000000000000000000AA"),
        json={"size_bytes": 1024},
    )
    assert response.status_code == 404


def test_get_returns_the_config_version(auth_client, uploaded_config_version, workspace):
    """A config version reads back by id, carrying its uploaded status."""
    response = auth_client.get(configs_url(workspace["workspace_id"], uploaded_config_version["config_version_id"]))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "uploaded"


def test_get_is_404_for_an_absent_config_version(auth_client, workspace):
    """A well formed id naming nothing is a 404."""
    response = auth_client.get(configs_url(workspace["workspace_id"], "cv-01JBQ0000000000000000000AA"))
    assert response.status_code == 404


def test_another_workspaces_config_version_reads_as_absent(auth_client, workspace, uploaded_config_version):
    """A version belonging to another workspace is a 404, not a 403.

    A 403 would confirm the id exists, which tells a caller about a workspace it
    cannot see. Absence is the safer answer and the true one from here.
    """
    other = auth_client.post(
        BASE,
        json={
            "name": "other",
            "engine": "terraform",
            "engine_version": "1.11.4",
            "run_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run",
        },
    ).json()
    response = auth_client.get(configs_url(other["workspace_id"], uploaded_config_version["config_version_id"]))
    assert response.status_code == 404


def test_list_returns_the_workspaces_config_versions(auth_client, workspace):
    """The list carries every version created against the workspace."""
    workspace_id = workspace["workspace_id"]
    auth_client.post(configs_url(workspace_id), json={"size_bytes": 1024})
    auth_client.post(configs_url(workspace_id), json={"size_bytes": 2048})
    response = auth_client.get(configs_url(workspace_id))
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2


def test_list_is_empty_for_a_new_workspace(auth_client, workspace):
    """A workspace starts with no config versions."""
    response = auth_client.get(configs_url(workspace["workspace_id"]))
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_list_is_404_for_an_absent_workspace(auth_client):
    """Listing against nothing is a 404 rather than an empty list."""
    assert auth_client.get(configs_url("ws-01JBQ0000000000000000000AA")).status_code == 404


def test_marking_uploaded_moves_the_status(auth_client, workspace):
    """The service marks a version uploaded, which is what gates a run."""
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(configs_url(workspace_id), json={"size_bytes": 1024}).json()["config_version"]
    workspaces_service.mark_config_version_uploaded(version["config_version_id"])
    body = auth_client.get(configs_url(workspace_id, version["config_version_id"])).json()
    assert body["status"] == "uploaded"
