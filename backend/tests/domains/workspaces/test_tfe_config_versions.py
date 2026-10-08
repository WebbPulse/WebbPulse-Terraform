"""The `tfe.v2` config versions the cloud backend creates, uploads to and polls.

go-tfe uploads with no Authorization and `application/octet-stream`, then reads the
version back until it says `uploaded`, so the URL must sign neither a type nor a
length and the read must reconcile against the bucket.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import boto3

from app.common.composition.settings import get_settings
from app.common.core.auth import CONFIGS_READ, WORKSPACES_READ
from app.common.db import repositories
from app.domains.workspaces import service as workspaces_service
from tests.conftest import ARTIFACTS_BUCKET, REGION

API = "/api/v2"
JSON_API = "application/vnd.api+json"


def _create(auth_client, workspace_id: str, **attributes):
    """POST a config version the way go-tfe does, with a JSON:API body."""
    body = {"data": {"type": "configuration-versions", "attributes": attributes}}
    return auth_client.post(
        f"{API}/workspaces/{workspace_id}/configuration-versions",
        content=json.dumps(body),
        headers={"Content-Type": JSON_API},
    )


def _land(key: str, body: bytes = b"tarball") -> None:
    """Put an object at `key`, the way the client's presigned PUT would."""
    boto3.client("s3", region_name=REGION).put_object(Bucket=ARTIFACTS_BUCKET, Key=key, Body=body)


def _row(config_version_id: str) -> dict:
    """The stored config version row."""
    item = repositories.config_versions(get_settings()).get({"config_version_id": config_version_id})
    assert item is not None
    return item


def test_create_renders_the_hcp_resource_with_an_upload_url(auth_client, workspace):
    """The create answer is the shape go-tfe decodes, carrying the upload URL."""
    response = _create(auth_client, workspace["workspace_id"], speculative=True, **{"auto-queue-runs": False})
    assert response.status_code == 201, response.text
    assert response.headers["content-type"].startswith(JSON_API)
    data = response.json()["data"]
    assert data["type"] == "configuration-versions"
    assert data["id"].startswith("cv-")
    attributes = data["attributes"]
    assert attributes["status"] == "pending"
    assert attributes["source"] == "tfe-api"
    assert attributes["speculative"] is True
    assert attributes["auto-queue-runs"] is False
    assert attributes["provisional"] is False
    assert attributes["error"] is None
    assert attributes["upload-url"].startswith("https://")
    assert data["relationships"]["ingress-attributes"] == {"data": None}


def test_a_provisional_version_reads_back_provisional(auth_client, workspace):
    """`terraform plan -out` uploads a provisional, non speculative version."""
    response = _create(auth_client, workspace["workspace_id"], speculative=False, provisional=True)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["attributes"]["provisional"] is True
    assert data["attributes"]["speculative"] is False
    assert _row(data["id"])["provisional"] is True


def test_create_writes_the_existing_config_version_row(auth_client, workspace):
    """The row is the same one `/api/v1` reads, keyed by the contract layout."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _create(auth_client, workspace_id).json()["data"]["id"]
    row = _row(config_version_id)
    assert row["workspace_id"] == workspace_id
    assert row["key"] == f"configs/{workspace_id}/{config_version_id}.tar.gz"
    assert row["status"] == "pending"
    assert row["source"] == "api"
    v1 = auth_client.get(f"/api/v1/workspaces/{workspace_id}/config-versions/{config_version_id}")
    assert v1.status_code == 200, v1.text


def test_the_upload_url_signs_no_content_type_or_length(auth_client, workspace):
    """go-tfe's PUT sends `application/octet-stream` and no Authorization, so only host is signed."""
    url = _create(auth_client, workspace["workspace_id"]).json()["data"]["attributes"]["upload-url"]
    query = parse_qs(urlparse(url).query)
    signed = query["X-Amz-SignedHeaders"][0].split(";")
    assert signed == ["host"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Expires"] == ["900"]


def test_read_is_pending_until_the_object_lands(auth_client, workspace):
    """The poll reads `pending` while nothing is in the bucket."""
    config_version_id = _create(auth_client, workspace["workspace_id"]).json()["data"]["id"]
    response = auth_client.get(f"{API}/configuration-versions/{config_version_id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["attributes"]["status"] == "pending"
    assert data["attributes"]["upload-url"] is None


def test_read_reconciles_to_uploaded(auth_client, workspace):
    """Once the object is there the poll reads `uploaded`, and the row is persisted."""
    config_version_id = _create(auth_client, workspace["workspace_id"]).json()["data"]["id"]
    _land(_row(config_version_id)["key"])
    response = auth_client.get(f"{API}/configuration-versions/{config_version_id}")
    assert response.status_code == 200
    assert response.json()["data"]["attributes"]["status"] == "uploaded"
    assert _row(config_version_id)["status"] == "uploaded"


def test_an_oversized_upload_is_errored_and_discarded(auth_client, workspace, monkeypatch):
    """An object over the ceiling reads `errored` and is deleted, so no run can use it."""
    monkeypatch.setattr(workspaces_service, "TFE_CONFIG_MAX_BYTES", 4)
    config_version_id = _create(auth_client, workspace["workspace_id"]).json()["data"]["id"]
    key = _row(config_version_id)["key"]
    _land(key, b"more than four bytes")
    response = auth_client.get(f"{API}/configuration-versions/{config_version_id}")
    assert response.status_code == 200
    attributes = response.json()["data"]["attributes"]
    assert attributes["status"] == "errored"
    assert attributes["error"] == "too_large"
    assert "250 MB" in attributes["error-message"]
    row = _row(config_version_id)
    assert row["status"] == "pending"
    assert row["upload_error"] == "too_large"
    listed = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=ARTIFACTS_BUCKET, Prefix=key)
    assert listed.get("KeyCount", 0) == 0
    again = auth_client.get(f"{API}/configuration-versions/{config_version_id}")
    assert again.json()["data"]["attributes"]["status"] == "errored"


def test_create_requires_configs_write(scoped_client, workspace):
    """A key with only read scopes cannot create a config version."""
    response = _create(scoped_client(WORKSPACES_READ, CONFIGS_READ), workspace["workspace_id"])
    assert response.status_code == 403
    assert response.json()["errors"][0]["status"] == "403"


def test_read_requires_configs_read(auth_client, scoped_client, workspace):
    """A key without `configs:read` cannot poll a config version."""
    config_version_id = _create(auth_client, workspace["workspace_id"]).json()["data"]["id"]
    response = scoped_client(WORKSPACES_READ).get(f"{API}/configuration-versions/{config_version_id}")
    assert response.status_code == 403


def test_create_against_an_absent_workspace_is_404(auth_client):
    """A config version cannot be created on a workspace that is not there."""
    response = _create(auth_client, "ws-01JBQ0000000000000000000AA")
    assert response.status_code == 404
    assert response.json()["errors"][0]["status"] == "404"


def test_read_of_an_absent_config_version_is_404(auth_client):
    """An unknown id is the JSON:API 404 go-tfe maps to not found."""
    response = auth_client.get(f"{API}/configuration-versions/cv-01JBQ0000000000000000000AA")
    assert response.status_code == 404
    assert response.json()["errors"][0]["status"] == "404"
