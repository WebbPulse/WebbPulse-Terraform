"""The upload route: who may publish, what version they publish, and retries."""

from __future__ import annotations

from urllib.parse import urlparse

from tests.domains.registry.conftest import OVERRIDE_REPO, claims, tarball


def test_tag_push_gets_a_presigned_put_for_its_module(upload):
    """The module comes from the repository name and the version from the tag."""
    response = upload(claims())

    assert response.status_code == 201
    body = response.json()
    assert body["upload_id"].startswith("up-")
    assert body["module"] == {"namespace": "WebbPulse", "name": "example", "provider": "aws", "version": "1.2.3"}
    assert urlparse(body["upload_url"]).path.endswith(f"registry/incoming/{body['upload_id']}.tar.gz")
    assert body["headers"]["Content-Type"] == "application/gzip"


def test_override_names_the_module_for_a_repository_off_convention(upload):
    """An allowlist value replaces the name and provider the repository name would give."""
    values = claims(repository=OVERRIDE_REPO, ref="refs/tags/0.1.0")

    response = upload(values)

    assert response.status_code == 201
    assert response.json()["module"]["name"] == "registry-proof"
    assert response.json()["module"]["provider"] == "null"


def test_repository_off_the_allowlist_is_refused(upload):
    """Only allowlisted repositories publish."""
    response = upload(claims(repository="WebbPulse/terraform-aws-other"))

    assert response.status_code == 403
    assert response.json()["error_code"] == "REGISTRY_REPO_NOT_ALLOWED"


def test_branch_push_is_refused(upload):
    """A version comes only from a tag push."""
    response = upload(claims(ref="refs/heads/main"))

    assert response.status_code == 422
    assert response.json()["error_code"] == "REGISTRY_REF_UNSUPPORTED"


def test_pull_request_event_is_refused(upload):
    """A pull request cannot publish even on a tag ref."""
    response = upload(claims(event_name="pull_request"))

    assert response.status_code == 422
    assert response.json()["error_code"] == "REGISTRY_REF_UNSUPPORTED"


def test_unverified_token_is_unauthorized(client):
    """A bearer the verifier rejects is a 401 with a challenge."""
    response = client.post(
        "/api/v1/registry/uploads",
        json={"size_bytes": 10},
        headers={"Authorization": "Bearer not-a-github-token"},
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_missing_bearer_is_unauthorized(client):
    """No token is a 401."""
    response = client.post("/api/v1/registry/uploads", json={"size_bytes": 10})

    assert response.status_code == 401


def test_token_missing_a_trusted_claim_is_unauthorized(upload):
    """A token without the sha cannot be attributed, so it is refused."""
    response = upload(claims(sha=""))

    assert response.status_code == 401


def test_oversized_upload_is_rejected(upload):
    """The declared size is bounded before anything is signed."""
    response = upload(claims(), size_bytes=100_000_001)

    assert response.status_code == 422


def test_retry_of_the_same_run_attempt_reuses_the_upload_id(upload):
    """A rerun of the same attempt signs the same key."""
    first = upload(claims()).json()["upload_id"]
    second = upload(claims()).json()["upload_id"]

    assert first == second


def test_new_run_attempt_takes_over_a_pending_version(upload):
    """A later attempt gets its own upload id for the same unpublished version."""
    first = upload(claims()).json()["upload_id"]
    second = upload(claims(run_attempt="2")).json()["upload_id"]

    assert first != second


def test_published_version_cannot_be_uploaded_again(upload, ingest):
    """Published versions are immutable."""
    upload_id = upload(claims()).json()["upload_id"]
    assert ingest(upload_id, tarball()) == "published"

    response = upload(claims(run_id="7001"))

    assert response.status_code == 409
    assert response.json()["error_code"] == "REGISTRY_VERSION_EXISTS"
