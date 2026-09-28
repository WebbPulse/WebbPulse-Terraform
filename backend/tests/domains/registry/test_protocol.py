"""The module registry protocol Terraform speaks, and the listing the SPA reads."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from app.common.core.auth import REGISTRY_READ, WORKSPACES_READ
from app.common.db import repositories
from app.domains.registry import service

VERSIONS = "/v1/modules/WebbPulse/example/aws/versions"


@pytest.fixture
def published(module, publish, settings):
    """Publish 1.2.3 and 1.10.0 of the example module and leave 2.0.0 pending."""
    for version in ("1.2.3", "1.10.0"):
        assert publish(version) == {"WebbPulse/example/aws": "published"}
    repositories.registry(settings).put(
        {
            "pk": service.module_pk("WebbPulse", "example", "aws"),
            "sk": service.version_sk("2.0.0"),
            "namespace": "WebbPulse",
            "name": "example",
            "provider": "aws",
            "version": "2.0.0",
            "status": service.PENDING,
            "sha": "e" * 40,
        }
    )


@pytest.fixture
def reader(scoped_client):
    """A client holding only `registry:read`, the scope a TF_TOKEN key carries."""
    with scoped_client(REGISTRY_READ) as client:
        yield client


def test_versions_lists_published_versions_newest_first(published, reader):
    """Pending versions are not offered to Terraform."""
    response = reader.get(VERSIONS)

    assert response.status_code == 200
    versions = [entry["version"] for entry in response.json()["modules"][0]["versions"]]
    assert versions == ["1.10.0", "1.2.3"]


def test_versions_match_the_address_case_insensitively(published, reader):
    """Terraform may spell the namespace in any case."""
    response = reader.get("/v1/modules/webbpulse/EXAMPLE/aws/versions")

    assert response.status_code == 200


def test_unknown_module_is_not_found(reader):
    """A module with no published versions is a 404."""
    response = reader.get("/v1/modules/WebbPulse/missing/aws/versions")

    assert response.status_code == 404


def test_download_answers_with_a_presigned_archive_url(published, reader):
    """Terraform follows `X-Terraform-Get`, an unauthenticated presigned GET."""
    response = reader.get("/v1/modules/WebbPulse/example/aws/1.2.3/download")

    assert response.status_code == 204
    location = urlparse(response.headers["X-Terraform-Get"])
    assert location.path.endswith("registry/modules/webbpulse/example/aws/1.2.3.tar.gz")
    assert "X-Amz-Signature" in location.query
    assert response.headers["Cache-Control"] == "no-store"


def test_download_of_a_pending_version_is_not_found(published, reader):
    """Only published versions download."""
    response = reader.get("/v1/modules/WebbPulse/example/aws/2.0.0/download")

    assert response.status_code == 404


def test_protocol_requires_credentials(published, client):
    """Only the discovery document is anonymous."""
    assert client.get(VERSIONS).status_code == 401
    assert client.get("/v1/modules/WebbPulse/example/aws/1.2.3/download").status_code == 401


def test_protocol_requires_the_registry_scope(published, scoped_client):
    """A key without `registry:read` cannot read modules."""
    with scoped_client(WORKSPACES_READ) as client:
        assert client.get(VERSIONS).status_code == 403


def test_listing_groups_versions_by_module(published, settings, reader):
    """The listing shows every module with its source address, its repository and each version's status."""
    repositories.registry(settings).put(
        {
            "pk": service.module_pk("WebbPulse", "legacy", "null"),
            "sk": service.version_sk("0.1.0"),
            "namespace": "WebbPulse",
            "name": "legacy",
            "provider": "null",
            "version": "0.1.0",
            "status": service.PUBLISHED,
        }
    )

    response = reader.get("/api/v1/registry/modules")

    assert response.status_code == 200
    modules = {module["name"]: module for module in response.json()["modules"]}
    assert set(modules) == {"example", "legacy"}
    example = modules["example"]
    assert example["source"].endswith("WebbPulse/example/aws")
    assert example["vcs_repo"] == "WebbPulse/terraform-aws-example"
    assert [(v["version"], v["status"]) for v in example["versions"]] == [
        ("2.0.0", "pending"),
        ("1.10.0", "published"),
        ("1.2.3", "published"),
    ]
    assert modules["legacy"]["vcs_repo"] is None


def test_listing_requires_the_registry_scope(scoped_client):
    """The listing sits behind the same scope as the protocol."""
    with scoped_client(WORKSPACES_READ) as client:
        assert client.get("/api/v1/registry/modules").status_code == 403
