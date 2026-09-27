"""Connecting a module to a repository, reading it back and deleting it."""

from __future__ import annotations

import boto3
import pytest

from app.common.core.auth import REGISTRY_READ, REGISTRY_WRITE
from app.common.db import repositories
from tests.domains.registry.conftest import INSTALLATION_ID, OTHER, REPO, REPOSITORY_ID

BASE = "/api/v1/registry/modules"


def test_connecting_names_the_module_by_convention(auth_client, github, settings):
    """`terraform-<provider>-<name>` gives the name and provider, the owner the namespace."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO.lower()})

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["namespace"], body["name"], body["provider"]) == ("WebbPulse", "example", "aws")
    assert body["vcs_repo"] == REPO
    assert body["versions"] == []
    row = repositories.registry(settings).get({"pk": "MODULE#webbpulse/example/aws", "sk": "MODULE"}) or {}
    assert row["vcs_repository_id"] == str(REPOSITORY_ID)
    assert row["vcs_installation_id"] == str(INSTALLATION_ID)


def test_connecting_takes_a_name_and_provider(auth_client, github):
    """A repository not named by convention names its module explicitly."""
    response = auth_client.post(BASE, json={"vcs_repo": OTHER["full_name"], "name": "network", "provider": "aws"})

    assert response.status_code == 201, response.text
    assert response.json()["source"] == "WebbPulse/network/aws"


def test_a_repository_off_convention_needs_a_name(auth_client, github):
    """Without a name and provider there is no address to publish under."""
    response = auth_client.post(BASE, json={"vcs_repo": OTHER["full_name"]})

    assert response.status_code == 422
    assert response.json()["error_code"] == "REGISTRY_INVALID_MODULE_NAME"


def test_a_second_module_at_the_same_address_conflicts(auth_client, module):
    """The address is the module's identity."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO})

    assert response.status_code == 409
    assert response.json()["error_code"] == "REGISTRY_MODULE_EXISTS"


def test_a_repository_the_app_cannot_see_is_a_422(auth_client, github):
    """Nothing is written, and the code names the fix."""
    response = auth_client.post(BASE, json={"vcs_repo": "someone/terraform-aws-else"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "VCS_REPO_NOT_INSTALLED"
    assert auth_client.get(BASE).json()["modules"] == []


def test_no_app_means_no_connection(auth_client):
    """An environment without the App cannot read a repository, so cannot publish from one."""
    response = auth_client.post(BASE, json={"vcs_repo": REPO})

    assert response.status_code == 409
    assert response.json()["error_code"] == "GITHUB_NOT_CONFIGURED"


@pytest.mark.parametrize(("failure", "expected"), [(500, 502), (429, 503)])
def test_a_github_failure_is_reported_as_unavailable(auth_client, github, failure, expected):
    """A GitHub fault is not the caller's fault."""
    github.failure = failure

    response = auth_client.post(BASE, json={"vcs_repo": REPO})

    assert response.status_code == expected
    assert response.json()["error_code"] == "GITHUB_UNAVAILABLE"


def test_get_returns_the_module_and_its_versions(auth_client, module, publish):
    """A connected module reads back with what its tags published."""
    publish("1.0.0")

    response = auth_client.get(f"{BASE}/WebbPulse/example/aws")

    assert response.status_code == 200
    body = response.json()
    assert body["vcs_repo"] == REPO
    assert [(v["version"], v["status"], v["tag"]) for v in body["versions"]] == [("1.0.0", "published", "v1.0.0")]


def test_get_of_an_unknown_module_is_not_found(auth_client):
    """Nothing at the address is a 404."""
    response = auth_client.get(f"{BASE}/WebbPulse/missing/aws")

    assert response.status_code == 404
    assert response.json()["error_code"] == "REGISTRY_NOT_FOUND"


def test_delete_removes_the_module_its_versions_and_its_tarballs(auth_client, module, publish, settings):
    """After a delete Terraform finds nothing and the bucket holds nothing for it."""
    publish("1.0.0")
    publish("1.1.0")

    response = auth_client.delete(f"{BASE}/webbpulse/example/aws")

    assert response.status_code == 204
    assert auth_client.get(f"{BASE}/WebbPulse/example/aws").status_code == 404
    listed = boto3.client("s3", region_name="us-west-2").list_objects_v2(
        Bucket=settings.ARTIFACTS_BUCKET, Prefix="registry/modules/webbpulse/example/aws/"
    )
    assert listed.get("KeyCount", 0) == 0
    assert auth_client.delete(f"{BASE}/WebbPulse/example/aws").status_code == 404


def test_a_deleted_module_publishes_nothing_more(auth_client, module, publish):
    """Once disconnected, a tag on the repository finds no module."""
    auth_client.delete(f"{BASE}/WebbPulse/example/aws")

    assert publish("2.0.0") == {}


def test_writes_require_the_registry_write_scope(scoped_client, module):
    """`registry:read` lists and reads but cannot connect or delete."""
    with scoped_client(REGISTRY_READ) as client:
        assert client.get(BASE).status_code == 200
        assert client.get(f"{BASE}/WebbPulse/example/aws").status_code == 200
        assert client.post(BASE, json={"vcs_repo": REPO}).status_code == 403
        assert client.delete(f"{BASE}/WebbPulse/example/aws").status_code == 403
    with scoped_client(REGISTRY_WRITE) as client:
        assert client.delete(f"{BASE}/WebbPulse/example/aws").status_code == 204


def test_writes_require_credentials(client):
    """No credentials, no module."""
    assert client.post(BASE, json={"vcs_repo": REPO}).status_code == 401
    assert client.delete(f"{BASE}/WebbPulse/example/aws").status_code == 401
