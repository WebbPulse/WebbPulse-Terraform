"""`POST /api/v1/vcs/uploads`: the GitHub Actions OIDC authenticated upload route.

What is pinned here is the contract `terraform-run.yml` v3.19.0 depends on: success
is exactly 201, a repository no workspace binds is a 404 carrying
`VCS_REPO_NOT_BOUND` at the top level, and a retried request from the same
workflow run attempt returns the same upload id and never writes a second record.
"""

from urllib.parse import urlparse

import boto3
import pytest
from vcs_helpers import BASE_SHA, HEAD_SHA, REPO, REPOSITORY_ID, FakeVerifier, claims, pr_claims, token_for

from app.common.db import repositories
from app.domains.runs import vcs

URL = "/api/v1/vcs/uploads"


@pytest.fixture
def verifier(monkeypatch):
    """The fake GitHub verifier the route resolves for any audience."""
    fake = FakeVerifier()
    monkeypatch.setattr(vcs, "_verifier", lambda audience: fake)
    return fake


@pytest.fixture
def bound(auth_client):
    """A workspace bound to the test repository, tracking `main`."""
    response = auth_client.post(
        "/api/v1/workspaces",
        json={
            "name": "bound",
            "engine_version": "1.11.4",
            "run_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run",
            "vcs_repo": REPO,
            "tracked_branch": "main",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _post(client, values, **body):
    """POST an upload request carrying a token for `values`."""
    payload = {"sha": HEAD_SHA, "pr_number": None, "base_sha": None, "size_bytes": 1024} | body
    return client.post(URL, json=payload, headers={"Authorization": f"Bearer {token_for(values)}"})


def _records(settings):
    """Every stored ingest record."""
    return list(repositories.vcs_uploads(settings).iter_scan())


def test_an_upload_is_exactly_201_with_a_presigned_put(client, verifier, bound, settings):
    """Success is 201 and names a key under `ingest/` for the upload id."""
    response = _post(client, claims())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["upload_id"].startswith("up-")
    path = urlparse(body["upload_url"]).path
    assert path.endswith(f"/ingest/{body['upload_id']}.tar.gz")
    assert body["headers"]["Content-Type"] == "application/gzip"
    assert body["headers"]["Content-Length"] == "1024"


def test_the_record_holds_the_verified_claims(client, verifier, bound, settings):
    """Repository, event, branch and commit come from the token, not the body."""
    response = _post(client, claims())
    [record] = _records(settings)
    assert record["upload_id"] == response.json()["upload_id"]
    assert record["repo"] == REPO
    assert record["repository_id"] == REPOSITORY_ID
    assert record["event"] == "push"
    assert record["branch"] == "main"
    assert record["sha"] == HEAD_SHA
    assert record["actor"] == "octocat"
    assert "pr_number" not in record
    assert int(record["expires_at"]) > 0
    assert len(record["claims_digest"]) == 64


def test_a_retry_returns_the_same_upload_id_and_writes_one_record(client, verifier, bound, settings):
    """The workflow retries on a 5xx: same run and attempt, same id, one record."""
    first = _post(client, claims())
    second = _post(client, claims())
    assert first.status_code == second.status_code == 201
    assert first.json()["upload_id"] == second.json()["upload_id"]
    assert len(_records(settings)) == 1


def test_a_new_attempt_is_a_new_upload(client, verifier, bound, settings):
    """A rerun of the workflow is a separate upload."""
    first = _post(client, claims())
    second = _post(client, claims(run_attempt="2"))
    assert first.json()["upload_id"] != second.json()["upload_id"]
    assert len(_records(settings)) == 2


def test_a_retry_with_a_new_size_moves_the_record_to_it(client, verifier, bound, settings):
    """The URL signs the size, so the record has to follow it."""
    _post(client, claims(), size_bytes=1024)
    second = _post(client, claims(), size_bytes=2048)
    assert second.json()["headers"]["Content-Length"] == "2048"
    [record] = _records(settings)
    assert int(record["size_bytes"]) == 2048


def test_an_unbound_repository_is_a_404_with_the_code_at_the_top_level(client, verifier, settings):
    """The workflow reads `error_code` at the top level and treats this as a notice."""
    response = _post(client, claims())
    assert response.status_code == 404
    assert response.json()["error_code"] == "VCS_REPO_NOT_BOUND"
    assert _records(settings) == []


def test_no_url_is_issued_to_an_unbound_repository(client, verifier, settings):
    """Nothing reaches the bucket's key space for a repository nobody bound."""
    response = _post(client, claims())
    assert "upload_url" not in response.json()


def test_a_missing_token_is_a_401(client, verifier, bound):
    """No bearer, no upload."""
    response = client.post(URL, json={"sha": HEAD_SHA, "size_bytes": 10})
    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


def test_a_token_that_fails_verification_is_a_401(client, verifier, bound):
    """A forged or expired token never reaches the binding check."""
    response = client.post(
        URL,
        json={"sha": HEAD_SHA, "size_bytes": 10},
        headers={"Authorization": "Bearer not-a-github-token"},
    )
    assert response.status_code == 401


def test_a_token_missing_a_trusted_claim_is_a_401(client, verifier, bound):
    """Every attribution claim is required."""
    values = claims()
    del values["repository_id"]
    assert _post(client, values).status_code == 401


def test_the_pull_request_number_comes_from_the_ref(client, verifier, bound, settings):
    """The body's number is ignored and its shas are kept only as unverified."""
    response = _post(client, pr_claims(7), pr_number=99, base_sha=BASE_SHA)
    assert response.status_code == 201, response.text
    [record] = _records(settings)
    assert record["event"] == "pull_request"
    assert int(record["pr_number"]) == 7
    assert record["sha"] != HEAD_SHA
    assert record["head_sha"] == HEAD_SHA
    assert record["base_sha"] == BASE_SHA
    assert "branch" not in record


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_name": "workflow_dispatch"},
        {"event_name": "pull_request_target", "ref": "refs/heads/main"},
        {"event_name": "push", "ref": "refs/tags/v1.0.0"},
        {"event_name": "pull_request", "ref": "refs/heads/feature"},
    ],
)
def test_an_event_that_starts_no_runs_is_a_422(client, verifier, bound, overrides, settings):
    """Only a branch push and a pull request's merge ref are accepted."""
    response = _post(client, claims(**overrides))
    assert response.status_code == 422
    assert response.json()["error_code"] == "VCS_EVENT_UNSUPPORTED"
    assert _records(settings) == []


def test_a_size_above_the_ceiling_is_a_422(client, verifier, bound):
    """The same ceiling as a config version."""
    assert _post(client, claims(), size_bytes=250_000_001).status_code == 422


def test_the_first_upload_records_the_repository_id(client, verifier, bound, settings):
    """From then on the binding follows the id rather than the name."""
    _post(client, claims())
    stored = repositories.workspaces(settings).get({"workspace_id": bound["workspace_id"]}) or {}
    assert stored["vcs_repository_id"] == REPOSITORY_ID


def test_a_renamed_repository_keeps_its_binding(client, verifier, bound):
    """The recorded id matches after the owner/name changes."""
    _post(client, claims())
    renamed = _post(client, claims(repository="WebbPulse/renamed-infra", run_id="9001"))
    assert renamed.status_code == 201


def test_a_new_repository_under_the_old_name_is_not_bound(client, verifier, bound):
    """A different id under the bound name does not inherit the binding."""
    _post(client, claims())
    impostor = _post(client, claims(repository_id="999", run_id="9001"))
    assert impostor.status_code == 404


def test_the_binding_is_case_insensitive_on_the_name(client, verifier, bound):
    """GitHub names are case insensitive."""
    response = _post(client, claims(repository=REPO.upper()))
    assert response.status_code == 201


def test_the_route_needs_no_identity_credential(client, verifier, bound):
    """The workflow holds only its GitHub token."""
    assert _post(client, claims()).status_code == 201


def test_the_route_is_served_by_the_runs_function(settings, verifier, bound):
    """The route lives on the runs domain, whose role holds the ingest table."""
    from fastapi.testclient import TestClient

    from app.common.composition.wiring import build_domain_app

    with TestClient(build_domain_app("runs", settings=settings)) as runs_client:
        assert _post(runs_client, claims()).status_code == 201


def test_the_upload_id_is_ulid_shaped():
    """The consumer's key pattern and the id generator agree."""
    import re

    assert re.fullmatch(vcs.UPLOAD_ID_PATTERN, vcs.upload_id_for(claims()))


def test_the_bucket_holds_nothing_until_the_workflow_puts(client, verifier, bound, settings):
    """Issuing a URL writes no object."""
    _post(client, claims())
    listed = boto3.client("s3", region_name="us-west-2").list_objects_v2(Bucket=settings.ARTIFACTS_BUCKET)
    assert listed.get("KeyCount", 0) == 0
