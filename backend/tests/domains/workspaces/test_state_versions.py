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
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import pytest

from app.common.core.auth import RUNS_READ, WORKSPACES_READ
from app.domains.workspaces import state_versions
from tests.conftest import REGION, STATE_BUCKET, WORKSPACE_PAYLOAD


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
    assert body["expires_in"] == state_versions.DOWNLOAD_EXPIRES_IN
    assert body["expires_in"] <= 60
    assert state_versions.state_key(workspace_id) in body["download_url"].replace("%2F", "/")
    assert "X-Amz-Signature" in body["download_url"]

    query = parse_qs(urlparse(body["download_url"]).query)
    assert query["versionId"] == [oldest]


def test_download_url_signature_binds_the_key_and_the_version(auth_client, versioned_state):
    """Tampering with the key or the version invalidates the signature.

    Moto does not verify presigned signatures, so following a tampered URL here
    would prove nothing. What real S3 enforces is that `versionId` is part of the
    signed query string, and the observable form of that is the signature moving
    when only the version moves. Without it a URL for an old version would serve
    whichever state is current, which is the failure that matters.
    """
    workspace_id = versioned_state["workspace_id"]
    oldest, newest = versioned_state["versions"][0], versioned_state["versions"][-1]

    def signature_for(version_id: str) -> str:
        response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/state-versions/{version_id}/download")
        assert response.status_code == 200, response.text
        return parse_qs(urlparse(response.json()["download_url"]).query)["X-Amz-Signature"][0]

    assert signature_for(oldest) != signature_for(newest)


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


def test_a_malformed_page_token_restarts_the_listing(auth_client, versioned_state):
    """A token that does not decode costs a repeated first page, not an error."""
    workspace_id = versioned_state["workspace_id"]

    response = auth_client.get(
        f"/api/v1/workspaces/{workspace_id}/state-versions",
        params={"page_token": "not-a-real-token"},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 3


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
    """The history routes reuse the workspace read scope rather than a new one.

    A separate `state:read` scope would be granted to every non-admin
    automatically, because the identity hook derives a non-admin's scopes from
    every name ending in `:read`. Reusing this one keeps state history exactly as
    reachable as the workspace it belongs to.
    """
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
