"""State version history: listing, pagination, metadata, download and its guard.

The history is S3's own object versioning rather than rows, so these tests write
real state objects into the moto bucket and assert against what the bucket then
reports. Versioning is turned on per test rather than in the shared fixture,
because only these tests depend on it and the rest of the suite should keep
writing single version objects.

The authorization tests are the point of the file. State is the most sensitive
thing the product stores, so there is a test that a caller without the workspace
read scope is refused the list, the metadata and the download, and a test that
the download refuses before it signs anything rather than after.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from app.common.core.auth import ALL_SCOPES, RUNNER_SCOPE, RUNS_READ, STATE_DOWNLOAD, WORKSPACES_READ
from app.common.identity.identity_hooks import ADMIN_ROLE, READ_SCOPES, scope_claim_for_roles
from app.domains.workspaces import state_versions
from tests.conftest import REGION, STATE_BUCKET, WORKSPACE_PAYLOAD


def storage_error(code: str, status: int, operation: str) -> ClientError:
    """Construct a complete SDK error response for storage failure tests."""
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": "test",
                "HostId": "test",
                "HTTPHeaders": {},
                "RetryAttempts": 0,
            },
        },
        operation,
    )


def enable_versioning() -> None:
    """Turn on object versioning for the state bucket, as the stack does."""
    boto3.client("s3", region_name=REGION).put_bucket_versioning(
        Bucket=STATE_BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )


def state_body(serial: int, terraform_version: str = "1.11.0") -> bytes:
    """A minimal but realistic state body, including a value that must never leak."""
    return json.dumps(
        {
            "version": 4,
            "terraform_version": terraform_version,
            "serial": serial,
            "lineage": "11111111-2222-3333-4444-555555555555",
            "outputs": {"db_password": {"value": "hunter2", "sensitive": True}},
            "resources": [
                {
                    "mode": "managed",
                    "type": "aws_db_instance",
                    "name": "main",
                    "instances": [{"attributes": {"password": "hunter2"}}],
                }
            ],
        }
    ).encode()


def write_state(workspace_id: str, serial: int, terraform_version: str = "1.11.0") -> str:
    """Write one state version for a workspace and return its S3 version id."""
    response = boto3.client("s3", region_name=REGION).put_object(
        Bucket=STATE_BUCKET,
        Key=state_versions.state_key(workspace_id),
        Body=state_body(serial, terraform_version),
        ContentType="application/json",
    )
    return str(response["VersionId"])


def write_lock(workspace_id: str) -> None:
    """Write the sibling `.tflock` object the S3 backend's native lock uses."""
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=STATE_BUCKET,
        Key=f"{state_versions.state_key(workspace_id)}.tflock",
        Body=b'{"ID":"lock"}',
    )


@pytest.fixture
def versioned_state(workspace) -> dict[str, Any]:
    """A workspace with three state versions written, newest last."""
    enable_versioning()
    workspace_id = workspace["workspace_id"]
    first = write_state(workspace_id, 1)
    second = write_state(workspace_id, 2)
    third = write_state(workspace_id, 3)
    return {"workspace_id": workspace_id, "versions": [first, second, third]}


def test_list_returns_every_version_newest_first(auth_client, versioned_state):
    """Three writes read back as three versions, with the newest marked current."""
    workspace_id = versioned_state["workspace_id"]
    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3
    assert [item["state_version_id"] for item in items] == list(reversed(versioned_state["versions"]))
    assert items[0]["is_current"] is True
    assert [item["is_current"] for item in items[1:]] == [False, False]
    assert all(item["size_bytes"] > 0 for item in items)
    assert all(item["workspace_id"] == workspace_id for item in items)


def test_list_never_returns_the_lock_object(auth_client, versioned_state):
    """The `.tflock` sibling shares the prefix and must not read as state history.

    The lock lives at `<state key>.tflock`, so a listing filtered by prefix alone
    would report every lock acquisition as a state version.
    """
    workspace_id = versioned_state["workspace_id"]
    write_lock(workspace_id)

    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3
    assert all(not item["state_version_id"].endswith("tflock") for item in items)


def test_list_paginates_and_the_token_continues(auth_client, versioned_state):
    """A page smaller than the history hands back a token that fetches the rest."""
    workspace_id = versioned_state["workspace_id"]

    first = auth_client.get(
        f"/api/v1/workspaces/{workspace_id}/state-versions",
        params={"page_size": 2},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_page_token"]

    second = auth_client.get(
        f"/api/v1/workspaces/{workspace_id}/state-versions",
        params={"page_size": 2, "page_token": first_body["next_page_token"]},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert len(second_body["items"]) == 1
    assert second_body["next_page_token"] is None

    seen = [item["state_version_id"] for item in first_body["items"] + second_body["items"]]
    assert seen == list(reversed(versioned_state["versions"]))
    assert len(set(seen)) == 3


def test_list_is_empty_for_a_workspace_that_never_ran(auth_client, workspace):
    """A workspace with no state object has an empty history, not a 404."""
    enable_versioning()
    workspace_id = workspace["workspace_id"]

    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_page_token": None}


def test_list_is_404_for_a_workspace_that_does_not_exist(auth_client):
    """An absent workspace is a 404 rather than an empty history."""
    response = auth_client.get("/api/v1/workspaces/ws-01ARZ3NDEKTSV4RRFFQ69G5FAV/state-versions")

    assert response.status_code == 404, response.text


def test_metadata_carries_the_serial_and_the_terraform_version(auth_client, versioned_state):
    """The three fields only the state body holds are read back for one version."""
    workspace_id = versioned_state["workspace_id"]
    oldest = versioned_state["versions"][0]

    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions/{oldest}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state_version_id"] == oldest
    assert body["serial"] == 1
    assert body["terraform_version"] == "1.11.0"
    assert body["lineage"] == "11111111-2222-3333-4444-555555555555"
    assert body["is_current"] is False
    assert body["size_bytes"] > 0


def test_metadata_never_returns_resources_or_outputs(auth_client, versioned_state):
    """The response carries no resource attribute and no output value.

    The stored state holds a password in both an output and a resource attribute.
    Neither may appear anywhere in the metadata response, under any key.
    """
    workspace_id = versioned_state["workspace_id"]
    newest = versioned_state["versions"][-1]

    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions/{newest}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "hunter2" not in response.text
    assert "resources" not in body
    assert "outputs" not in body
    assert body["is_current"] is True


def test_metadata_is_404_for_a_version_of_another_workspace(auth_client, versioned_state):
    """A version id from one workspace cannot be read through another's path.

    Both workspaces are readable by this caller, so this is not a scope check: it
    is the check that a version id is only meaningful against the key it belongs
    to, which is what stops one workspace's state being fetched through another.
    """
    other = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"name": "other"})
    assert other.status_code == 201, other.text
    other_id = other.json()["workspace_id"]
    write_state(other_id, 1)

    borrowed = versioned_state["versions"][0]
    response = auth_client.get(f"/api/v1/workspaces/{other_id}/state-versions/{borrowed}")

    assert response.status_code == 404, response.text


def test_download_returns_a_short_lived_url_pinned_to_the_version(auth_client, versioned_state):
    """The URL names this key and this version, and expires quickly."""
    workspace_id = versioned_state["workspace_id"]
    oldest = versioned_state["versions"][0]

    response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/{oldest}/download")

    assert response.status_code == 200, response.text
    body = response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert body["expires_in"] == state_versions.DOWNLOAD_EXPIRES_IN
    assert body["expires_in"] <= 60
    assert state_versions.state_key(workspace_id) in body["download_url"].replace("%2F", "/")
    assert "X-Amz-Signature" in body["download_url"]

    query = parse_qs(urlparse(body["download_url"]).query)
    assert query["versionId"] == [oldest]
    assert query["response-cache-control"] == ["no-store"]
    assert query["response-content-type"] == ["application/json"]
    assert query["response-content-disposition"][0].startswith("attachment;")


def test_download_url_signature_binds_the_key_and_the_version(auth_client, versioned_state, monkeypatch):
    """Tampering with the key or the version invalidates the signature.

    Moto does not verify presigned signatures, so following a tampered URL here
    would prove nothing. What real S3 enforces is that `versionId` is part of the
    signed query string, and the observable form of that is the signature moving
    when only the version moves. Without it a URL for an old version would serve
    whichever state is current, which is the failure that matters.
    """
    workspace_id = versioned_state["workspace_id"]
    oldest, newest = versioned_state["versions"][0], versioned_state["versions"][-1]
    monkeypatch.setattr("botocore.auth.get_current_datetime", lambda: datetime(2026, 9, 21, tzinfo=timezone.utc))

    def signature_for(version_id: str) -> str:
        """Read only the signature from a mocked state download."""
        response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/{version_id}/download")
        assert response.status_code == 200, response.text
        return parse_qs(urlparse(response.json()["download_url"]).query)["X-Amz-Signature"][0]

    assert signature_for(oldest) != signature_for(newest)

    first = signature_for(oldest)
    other = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"name": "signature-binding"})
    assert other.status_code == 201
    workspace_id = other.json()["workspace_id"]
    monkeypatch.setattr(state_versions, "_head", lambda *_args, **_kwargs: {"ContentLength": 1})
    assert signature_for(oldest) != first


def test_download_is_404_for_a_version_that_does_not_exist(auth_client, versioned_state):
    """An unknown version id mints nothing."""
    workspace_id = versioned_state["workspace_id"]

    response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/nosuchversionid/download")

    assert response.status_code == 404, response.text
    assert "X-Amz-Signature" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workspaces/{workspace_id}/state-versions",
        "/api/v1/workspaces/{workspace_id}/state-versions/{version}",
    ],
)
def test_reading_history_needs_the_workspace_read_scope(scoped_client, versioned_state, path):
    """A caller that cannot read the workspace cannot read its state history.

    The guard is the same scope that gates reading the workspace itself, so there
    is no second authorization model to keep in step with the first.
    """
    workspace_id = versioned_state["workspace_id"]
    version = versioned_state["versions"][0]
    target = path.format(workspace_id=workspace_id, version=version)

    with scoped_client(RUNS_READ) as client:
        response = client.get(target)

    assert response.status_code == 403, response.text


def test_download_needs_the_workspace_read_scope(scoped_client, versioned_state):
    """A caller without the scope is refused the download outright."""
    workspace_id = versioned_state["workspace_id"]
    version = versioned_state["versions"][0]

    with scoped_client(RUNS_READ) as client:
        response = client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/{version}/download")

    assert response.status_code == 403, response.text
    assert "X-Amz-Signature" not in response.text


def test_download_checks_authorization_before_minting_any_url(scoped_client, versioned_state, monkeypatch):
    """No URL is signed for a caller the scope guard refuses.

    A 403 alone would not prove this: the guard could run after a URL had already
    been minted and simply discarded, which would still have handed a bearer
    credential to whatever logs or traces the signing. This replaces the signer
    with one that fails the test if it is reached at all.
    """
    workspace_id = versioned_state["workspace_id"]
    version = versioned_state["versions"][0]

    def forbidden_presigner(*_args: Any, **_kwargs: Any) -> Any:
        """Fail loudly: reaching this means a URL was minted before the guard ran."""
        raise AssertionError("A state download URL was minted before authorization was checked.")

    monkeypatch.setattr(state_versions, "_presigner", forbidden_presigner)

    with scoped_client(RUNS_READ) as client:
        response = client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/{version}/download")

    assert response.status_code == 403, response.text


def test_download_checks_the_version_belongs_to_the_workspace_before_signing(auth_client, versioned_state, monkeypatch):
    """An authorized caller still signs nothing for a version that is not there.

    The ownership check is what keeps a valid scope from being enough to reach an
    arbitrary version id, so it has to run before the signing too.
    """
    workspace_id = versioned_state["workspace_id"]

    def forbidden_presigner(*_args: Any, **_kwargs: Any) -> Any:
        """Fail loudly: reaching this means an unknown version was signed."""
        raise AssertionError("A state download URL was minted for an unknown version.")

    monkeypatch.setattr(state_versions, "_presigner", forbidden_presigner)

    response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/nosuchversionid/download")

    assert response.status_code == 404, response.text


def test_download_records_the_access(auth_client, versioned_state, caplog):
    """A state download leaves a line naming who reached which version.

    The repository has no audit store, so this asserts the structured log the
    route writes instead, and that the URL itself never appears in it.
    """
    workspace_id = versioned_state["workspace_id"]
    version = versioned_state["versions"][0]

    with caplog.at_level("INFO", logger="app.domains.workspaces.router"):
        response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/{version}/download")

    assert response.status_code == 200, response.text
    recorded = [
        record for record in caplog.records if getattr(record, "event", "") == "workspaces.state_version.download"
    ]
    assert len(recorded) == 1
    assert getattr(recorded[0], "workspace_id") == workspace_id
    assert getattr(recorded[0], "state_version_id") == version
    assert getattr(recorded[0], "subject")
    assert "X-Amz-Signature" not in caplog.text


def test_page_size_is_bounded(auth_client, versioned_state):
    """A page size past the ceiling is refused rather than passed to S3."""
    workspace_id = versioned_state["workspace_id"]

    response = auth_client.get(
        f"/api/v1/workspaces/{workspace_id}/state-versions",
        params={"page_size": state_versions.MAX_PAGE_SIZE + 1},
    )

    assert response.status_code == 422, response.text


def test_a_malformed_page_token_is_rejected(auth_client, versioned_state):
    """A broken cursor cannot silently restart a client's pagination loop."""
    workspace_id = versioned_state["workspace_id"]

    response = auth_client.get(
        f"/api/v1/workspaces/{workspace_id}/state-versions",
        params={"page_token": "not-a-real-token"},
    )

    assert response.status_code == 400


def test_metadata_tolerates_a_body_that_is_not_state(auth_client, workspace):
    """An unparseable body still describes: empty serial, not a 500."""
    enable_versioning()
    workspace_id = workspace["workspace_id"]
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=STATE_BUCKET,
        Key=state_versions.state_key(workspace_id),
        Body=b"not json at all",
    )

    listed = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions")
    assert listed.status_code == 200, listed.text
    version = listed.json()["items"][0]["state_version_id"]

    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions/{version}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["serial"] is None
    assert body["terraform_version"] is None


def test_the_scope_is_the_same_one_that_reads_the_workspace():
    """Every history route requires workspace read, including raw downloads."""
    from app.domains.workspaces import router as workspaces_router

    routes = [route for route in workspaces_router.router.routes if "state-versions" in getattr(route, "path", "")]
    assert routes

    for route in routes:
        guarded = False
        for dependency in getattr(route, "dependencies", []):
            call = getattr(dependency, "dependency", None)
            if call is None:
                continue
            closure = getattr(call, "__closure__", None) or ()
            for cell in closure:
                contents = cell.cell_contents
                if isinstance(contents, tuple) and WORKSPACES_READ in contents:
                    guarded = True
        assert guarded, getattr(route, "path", "")


@pytest.mark.parametrize(
    "granted",
    [
        READ_SCOPES,
        (WORKSPACES_READ,),
        (STATE_DOWNLOAD,),
        (RUNNER_SCOPE,),
        tuple(scope for scope in ALL_SCOPES if scope != STATE_DOWNLOAD),
    ],
)
def test_download_requires_both_explicit_scopes(scoped_client, versioned_state, monkeypatch, granted):
    """No implicit role or partial scope set grants access to raw state."""
    signer = Mock(side_effect=AssertionError("Unauthorized signing"))
    reader = Mock(side_effect=AssertionError("Unauthorized state access"))
    monkeypatch.setattr(state_versions, "_presigner", signer)
    monkeypatch.setattr(state_versions, "_s3", reader)
    path = f"/api/v1/workspaces/{versioned_state['workspace_id']}/state-versions/{versioned_state['versions'][0]}"
    with scoped_client(*granted) as caller:
        assert caller.post(f"{path}/download").status_code == 403
    signer.assert_not_called()
    reader.assert_not_called()


def test_download_accepts_only_the_two_required_scopes(scoped_client, versioned_state):
    """An explicitly delegated download works without write or apply permission."""
    path = f"/api/v1/workspaces/{versioned_state['workspace_id']}/state-versions/{versioned_state['versions'][0]}"
    with scoped_client(WORKSPACES_READ, STATE_DOWNLOAD) as caller:
        assert caller.post(f"{path}/download").status_code == 200


def test_download_scope_is_admin_only_by_default():
    """Ordinary human sessions cannot inherit raw state access through read scopes."""
    assert STATE_DOWNLOAD not in scope_claim_for_roles([]).split()
    assert STATE_DOWNLOAD in scope_claim_for_roles([ADMIN_ROLE]).split()


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/version"), ("POST", "/version/download")])
def test_state_routes_reject_anonymous_access(client, workspace, method, suffix):
    """Every state route authenticates before touching state storage."""
    path = f"/api/v1/workspaces/{workspace['workspace_id']}/state-versions{suffix}"
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize("markers", [("other/key", "version"), ("", "version"), ("key", ""), ("key", 1)])
def test_foreign_or_incomplete_cursor_never_reaches_s3(auth_client, versioned_state, monkeypatch, markers):
    """Cursor markers must be a complete pair bound to this exact workspace key."""
    reader = Mock(side_effect=AssertionError("Invalid cursor reached S3"))
    monkeypatch.setattr(state_versions, "_s3", reader)
    path = f"/api/v1/workspaces/{versioned_state['workspace_id']}/state-versions"
    token = state_versions.encode_page_token(*markers)
    assert auth_client.get(path, params={"page_token": token}).status_code == 400
    reader.assert_not_called()


def test_pagination_crosses_delete_markers_and_stops_at_siblings(auth_client, versioned_state):
    """Empty pages from delete markers preserve continuation without listing locks."""
    workspace_id = versioned_state["workspace_id"]
    s3 = boto3.client("s3", region_name=REGION)
    s3.delete_object(Bucket=STATE_BUCKET, Key=state_versions.state_key(workspace_id))
    for _ in range(3):
        write_lock(workspace_id)
    path = f"/api/v1/workspaces/{workspace_id}/state-versions"
    params: dict[str, Any] = {"page_size": 1}
    seen = []
    tokens = set()
    for _ in range(6):
        response = auth_client.get(path, params=params)
        assert response.status_code == 200
        page = response.json()
        seen.extend(item["state_version_id"] for item in page["items"])
        assert all(not item["is_current"] for item in page["items"])
        token = page["next_page_token"]
        if token is None:
            break
        assert token not in tokens
        tokens.add(token)
        params["page_token"] = token
    else:
        pytest.fail("State history did not terminate")
    assert seen == list(reversed(versioned_state["versions"]))


@pytest.mark.parametrize("download", [False, True])
def test_delete_marker_is_not_a_state_version(auth_client, versioned_state, monkeypatch, download):
    """S3's 405 for a specific delete marker is a missing state, never a 500."""
    reader = Mock()
    reader.head_object.side_effect = storage_error("MethodNotAllowed", 405, "HeadObject")
    signer = Mock(side_effect=AssertionError("Delete marker was signed"))
    monkeypatch.setattr(state_versions, "_s3", lambda _: reader)
    monkeypatch.setattr(state_versions, "_presigner", signer)
    path = f"/api/v1/workspaces/{versioned_state['workspace_id']}/state-versions/delete-marker"
    response = auth_client.post(f"{path}/download") if download else auth_client.get(path)
    assert response.status_code == 404
    signer.assert_not_called()


def test_metadata_read_is_bounded_and_closes_stream(workspace, settings, monkeypatch):
    """Even a body larger than HEAD reported is bounded and its connection closed."""
    ceiling = 32
    stream = BytesIO(b"x" * (ceiling + 10))
    reader = Mock()
    reader.head_object.return_value = {"ContentLength": 1, "VersionId": "version"}
    reader.get_object.return_value = {"Body": StreamingBody(stream, ceiling + 10)}
    monkeypatch.setattr(state_versions, "METADATA_READ_CEILING", ceiling)
    monkeypatch.setattr(state_versions, "_s3", lambda _: reader)
    result = state_versions.get_state_version(workspace["workspace_id"], "version", settings=settings)
    assert stream.closed
    assert result["serial"] is None


@pytest.mark.parametrize("operation", ["head_object", "get_object"])
def test_storage_access_denied_is_not_success_or_not_found(workspace, settings, monkeypatch, operation):
    """A deployment permission failure must not look like valid or missing metadata."""
    reader = Mock()
    reader.head_object.return_value = {"ContentLength": 1, "VersionId": "version"}
    getattr(reader, operation).side_effect = storage_error("AccessDenied", 403, operation)
    monkeypatch.setattr(state_versions, "_s3", lambda _: reader)
    with pytest.raises(ClientError):
        state_versions.get_state_version(workspace["workspace_id"], "version", settings=settings)


def test_listing_never_reads_a_state_body(auth_client, versioned_state, monkeypatch):
    """Metadata pages cannot accidentally decrypt or buffer any state contents."""
    reader = boto3.client("s3", region_name=REGION)
    forbidden = Mock(side_effect=AssertionError("List read raw state"))
    monkeypatch.setattr(reader, "get_object", forbidden)
    monkeypatch.setattr(state_versions, "_s3", lambda _: reader)
    response = auth_client.get(f"/api/v1/workspaces/{versioned_state['workspace_id']}/state-versions")
    assert response.status_code == 200
    forbidden.assert_not_called()


def test_download_rejects_another_workspaces_version(auth_client, versioned_state, monkeypatch):
    """A valid version on another key cannot be signed through this workspace."""
    other = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"name": "other-download"})
    assert other.status_code == 201
    signer = Mock(side_effect=AssertionError("Foreign state was signed"))
    monkeypatch.setattr(state_versions, "_presigner", signer)
    path = f"/api/v1/workspaces/{other.json()['workspace_id']}/state-versions/{versioned_state['versions'][0]}/download"
    assert auth_client.post(path).status_code == 404
    signer.assert_not_called()


def test_storage_rejects_an_invalid_version_cursor(auth_client, workspace, monkeypatch):
    """A syntactically valid cursor rejected by S3 is a client error."""
    reader = Mock()
    reader.list_object_versions.side_effect = storage_error("InvalidArgument", 400, "ListObjectVersions")
    monkeypatch.setattr(state_versions, "_s3", lambda _: reader)
    workspace_id = workspace["workspace_id"]
    token = state_versions.encode_page_token(state_versions.state_key(workspace_id), "invalid")
    response = auth_client.get(f"/api/v1/workspaces/{workspace_id}/state-versions", params={"page_token": token})
    assert response.status_code == 400


def test_scopes_hold_in_the_deployed_workspaces_composition(settings, versioned_state):
    """The isolated Lambda composition enforces the same raw state boundary."""
    from fastapi.testclient import TestClient

    from app.common.composition.wiring import build_domain_app
    from tests.conftest import mint_key

    app = build_domain_app("workspaces", settings=settings)
    path = f"/api/v1/workspaces/{versioned_state['workspace_id']}/state-versions/{versioned_state['versions'][0]}"
    with TestClient(app, headers={"Authorization": f"Bearer {mint_key(*READ_SCOPES)}"}) as caller:
        assert caller.get(path).status_code == 200
        assert caller.post(f"{path}/download").status_code == 403
    with TestClient(app, headers={"Authorization": f"Bearer {mint_key(WORKSPACES_READ, STATE_DOWNLOAD)}"}) as caller:
        assert caller.post(f"{path}/download").status_code == 200
