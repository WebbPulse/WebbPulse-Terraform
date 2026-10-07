"""The `tfe.v2` state versions and outputs behind `terraform state`, `import` and `output`.

The cloud backend reads the current state version, follows its download URL, reads
outputs, and writes a new version with inline base64 state while it holds the lock.
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import boto3
import pytest

from app.common.core.auth import STATE_DOWNLOAD, STATE_WRITE, WORKSPACES_READ
from app.domains.workspaces import tfe_state
from app.domains.workspaces.state_versions import state_key
from tests.conftest import REGION, STATE_BUCKET

API = "/api/v2"
JSON_API = "application/vnd.api+json"
LINEAGE = "6b3b3c3e-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _versioned_state_bucket(aws_environment) -> None:
    """Turn on object versioning for the state bucket, as the stack does, since every id names a version."""
    boto3.client("s3", region_name=REGION).put_bucket_versioning(
        Bucket=STATE_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )


def _state(serial: int, *, lineage: str = LINEAGE, outputs: dict | None = None) -> bytes:
    """A minimal version 4 state document."""
    body = {
        "version": 4,
        "terraform_version": "1.16.4",
        "serial": serial,
        "lineage": lineage,
        "outputs": outputs or {},
        "resources": [],
        "check_results": None,
    }
    return json.dumps(body, indent=2).encode()


def _attributes(content: bytes, **overrides) -> dict:
    """The create attributes go-tfe sends for `content`."""
    parsed = json.loads(content)
    attributes = {
        "serial": parsed["serial"],
        "md5": hashlib.md5(content).hexdigest(),
        "lineage": parsed["lineage"],
        "state": base64.b64encode(content).decode(),
        "force": False,
    }
    attributes.update(overrides)
    return attributes


def _create(client, workspace_id: str, attributes: dict):
    """POST a state version the way go-tfe's inline create does."""
    body = {"data": {"type": "state-versions", "attributes": attributes}}
    return client.post(
        f"{API}/workspaces/{workspace_id}/state-versions",
        content=json.dumps(body),
        headers={"Content-Type": JSON_API},
    )


def _lock(client, workspace_id: str) -> None:
    """Take the CLI lock, which a state write requires."""
    response = client.post(f"{API}/workspaces/{workspace_id}/actions/lock", content="{}")
    assert response.status_code == 200, response.text


def _seed(workspace_id: str, content: bytes) -> str:
    """Write state the way a run's engine does and return its version id."""
    written = boto3.client("s3", region_name=REGION).put_object(
        Bucket=STATE_BUCKET, Key=state_key(workspace_id), Body=content
    )
    return written["VersionId"]


def test_ids_round_trip():
    """A state version id and an output id each name their workspace with no lookup."""
    workspace_id = "ws-01JBQ0000000000000000000AA"
    sv = tfe_state.state_version_id(workspace_id, "abc.DEF_123-x")
    assert sv.startswith("sv-")
    assert tfe_state.parse_state_version_id(sv) == (workspace_id, "abc.DEF_123-x")
    out = tfe_state.output_id(workspace_id, "abc", "endpoint")
    assert out.startswith("wsout-")
    assert "/" not in out and "=" not in out
    assert tfe_state.parse_output_id(out) == (workspace_id, "abc", "endpoint")


def test_no_state_yet_is_404(auth_client, workspace):
    """go-tfe reads a 404 as an empty workspace, which `terraform state list` prints as nothing."""
    response = auth_client.get(f"{API}/workspaces/{workspace['workspace_id']}/current-state-version")
    assert response.status_code == 404
    assert response.json()["errors"][0]["status"] == "404"


def test_current_state_version_renders_the_runners_object(auth_client, workspace):
    """The current version is the object a run wrote, with its serial and download link."""
    workspace_id = workspace["workspace_id"]
    version_id = _seed(workspace_id, _state(3))
    response = auth_client.get(f"{API}/workspaces/{workspace_id}/current-state-version")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["type"] == "state-versions"
    assert data["id"] == tfe_state.state_version_id(workspace_id, version_id)
    attributes = data["attributes"]
    assert attributes["serial"] == 3
    assert attributes["terraform-version"] == "1.16.4"
    assert attributes["status"] == "finalized"
    assert attributes["hosted-state-download-url"] == f"/api/v2/state-versions/{data['id']}/download"
    by_id = auth_client.get(f"{API}/state-versions/{data['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["data"]["id"] == data["id"]


def test_download_redirects_to_a_pinned_presigned_get(auth_client, workspace):
    """The download is a 307 to S3, so the bearer never reaches the bucket."""
    workspace_id = workspace["workspace_id"]
    version_id = _seed(workspace_id, _state(1))
    sv = tfe_state.state_version_id(workspace_id, version_id)
    response = auth_client.get(f"{API}/state-versions/{sv}/download", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["cache-control"] == "no-store"
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    assert query["versionId"] == [version_id]
    assert query["X-Amz-Expires"] == ["60"]


def test_reads_require_state_download(scoped_client, workspace):
    """Reading state is `state:download`, never plain workspace reads."""
    workspace_id = workspace["workspace_id"]
    version_id = _seed(workspace_id, _state(1))
    client = scoped_client(WORKSPACES_READ)
    sv = tfe_state.state_version_id(workspace_id, version_id)
    for path in (
        f"/workspaces/{workspace_id}/current-state-version",
        f"/workspaces/{workspace_id}/current-state-version-outputs",
        f"/state-versions/{sv}",
        f"/state-versions/{sv}/download",
    ):
        response = client.get(f"{API}{path}", follow_redirects=False)
        assert response.status_code == 403, path


def test_outputs_carry_detailed_types_and_withhold_sensitive_values(auth_client, workspace):
    """The list hides a sensitive value; the single read returns it, as HCP does."""
    workspace_id = workspace["workspace_id"]
    outputs = {
        "endpoint": {"value": "https://example.test", "type": "string"},
        "secret": {"value": "hunter2", "type": "string", "sensitive": True},
        "ports": {"value": [80, 443], "type": ["list", "number"]},
    }
    _seed(workspace_id, _state(2, outputs=outputs))
    response = auth_client.get(f"{API}/workspaces/{workspace_id}/current-state-version-outputs")
    assert response.status_code == 200, response.text
    body = response.json()
    by_name = {item["attributes"]["name"]: item for item in body["data"]}
    assert set(by_name) == {"endpoint", "secret", "ports"}
    assert body["meta"]["pagination"]["total-count"] == 3
    assert by_name["endpoint"]["attributes"]["value"] == "https://example.test"
    assert by_name["endpoint"]["attributes"]["detailed-type"] == "string"
    assert by_name["ports"]["attributes"]["detailed-type"] == ["list", "number"]
    assert by_name["ports"]["attributes"]["type"] == "array"
    assert by_name["secret"]["attributes"]["sensitive"] is True
    assert by_name["secret"]["attributes"]["value"] is None
    single = auth_client.get(f"{API}/state-version-outputs/{by_name['secret']['id']}")
    assert single.status_code == 200, single.text
    assert single.json()["data"]["attributes"]["value"] == "hunter2"


def test_an_unknown_output_is_404(auth_client, workspace):
    """An id naming no output is the JSON:API 404."""
    workspace_id = workspace["workspace_id"]
    version_id = _seed(workspace_id, _state(1))
    missing = tfe_state.output_id(workspace_id, version_id, "nope")
    assert auth_client.get(f"{API}/state-version-outputs/{missing}").status_code == 404
    assert auth_client.get(f"{API}/state-version-outputs/wsout-bm90LWpzb24").status_code == 404


def test_create_writes_the_runners_object_under_the_lock(auth_client, workspace):
    """A locked caller's state lands on the one state key, readable as the current version."""
    workspace_id = workspace["workspace_id"]
    _lock(auth_client, workspace_id)
    content = _state(1)
    response = _create(auth_client, workspace_id, _attributes(content))
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["attributes"]["serial"] == 1
    obj = boto3.client("s3", region_name=REGION).get_object(Bucket=STATE_BUCKET, Key=state_key(workspace_id))
    assert obj["Body"].read() == content
    assert obj["ServerSideEncryption"] == "aws:kms"
    current = auth_client.get(f"{API}/workspaces/{workspace_id}/current-state-version")
    assert current.json()["data"]["id"] == data["id"]


def test_create_without_state_asks_go_tfe_to_send_it_inline(auth_client, workspace):
    """This wording is what makes go-tfe fall back from the upload path to inline state."""
    workspace_id = workspace["workspace_id"]
    _lock(auth_client, workspace_id)
    attributes = _attributes(_state(1))
    del attributes["state"]
    response = _create(auth_client, workspace_id, attributes)
    assert response.status_code == 422
    assert "param is missing or the value is empty: state" in response.json()["errors"][0]["detail"]


def test_create_without_the_lock_is_409(auth_client, workspace):
    """Only the lock holder writes, so no run's engine can be writing at the same time."""
    response = _create(auth_client, workspace["workspace_id"], _attributes(_state(1)))
    assert response.status_code == 409


def test_create_rejects_a_bad_md5(auth_client, workspace):
    """Bytes that do not match their md5 are a 422."""
    workspace_id = workspace["workspace_id"]
    _lock(auth_client, workspace_id)
    response = _create(auth_client, workspace_id, _attributes(_state(1), md5="0" * 32))
    assert response.status_code == 422


def test_serial_must_move_forward(auth_client, workspace):
    """An older serial, or new bytes at the same serial, would lose state, so they are refused."""
    workspace_id = workspace["workspace_id"]
    _seed(workspace_id, _state(5))
    _lock(auth_client, workspace_id)
    assert _create(auth_client, workspace_id, _attributes(_state(4))).status_code == 409
    changed = _state(5, outputs={"x": {"value": "y", "type": "string"}})
    assert _create(auth_client, workspace_id, _attributes(changed)).status_code == 409
    assert _create(auth_client, workspace_id, _attributes(_state(6))).status_code == 201


def test_the_same_bytes_at_the_same_serial_write_nothing(auth_client, workspace):
    """A repeat of the current state answers with the current version rather than a new one."""
    workspace_id = workspace["workspace_id"]
    content = _state(5)
    version_id = _seed(workspace_id, content)
    _lock(auth_client, workspace_id)
    response = _create(auth_client, workspace_id, _attributes(content))
    assert response.status_code == 201, response.text
    assert response.json()["data"]["id"] == tfe_state.state_version_id(workspace_id, version_id)


def test_a_different_lineage_needs_force(auth_client, workspace):
    """Replacing unrelated state is `terraform state push -force` and nothing else."""
    workspace_id = workspace["workspace_id"]
    _seed(workspace_id, _state(5))
    _lock(auth_client, workspace_id)
    other = _state(1, lineage="6b3b3c3e-0000-4000-8000-000000000002")
    assert _create(auth_client, workspace_id, _attributes(other)).status_code == 409
    assert _create(auth_client, workspace_id, _attributes(other, force=True)).status_code == 201


def test_create_requires_state_write(scoped_client, workspace):
    """Writing state is `state:write`; `state:download` alone cannot."""
    client = scoped_client(WORKSPACES_READ, STATE_DOWNLOAD)
    response = _create(client, workspace["workspace_id"], _attributes(_state(1)))
    assert response.status_code == 403


def test_state_write_with_the_lock_may_write(scoped_client, workspace):
    """The login key's state scopes are enough for `terraform state mv`."""
    client = scoped_client(WORKSPACES_READ, STATE_DOWNLOAD, STATE_WRITE)
    workspace_id = workspace["workspace_id"]
    _lock(client, workspace_id)
    assert _create(client, workspace_id, _attributes(_state(1))).status_code == 201
