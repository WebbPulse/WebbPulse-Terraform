"""The tag consumer: a semantic version tag publishing the tagged commit of each connected module."""

from __future__ import annotations

import io
import json
import tarfile

import boto3
import pytest

from app.common.db import repositories
from app.common.github.archive import TarballUnavailable
from app.common.github.webhooks import MalformedDelivery
from app.domains.registry import service
from app.domains.registry.consumers import dispatch, tags
from tests.domains.registry.conftest import SHA, github_tarball, tag_record

ADDRESS = "WebbPulse/example/aws"


def _row(settings, version: str) -> dict:
    """The stored version row."""
    key = {"pk": service.module_pk("WebbPulse", "example", "aws"), "sk": service.version_sk(version)}
    return repositories.registry(settings).get(key) or {}


def _members(settings, version: str) -> list[str]:
    """The names inside the stored module tarball."""
    body = boto3.client("s3", region_name="us-west-2").get_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=service.module_key("WebbPulse", "example", "aws", version)
    )["Body"]
    with tarfile.open(fileobj=io.BytesIO(body.read()), mode="r:gz") as archive:
        return sorted(archive.getnames())


def test_a_tag_publishes_the_module_at_its_root(module, publish, github, settings):
    """GitHub's top directory is stripped, so Terraform finds the root module."""
    assert publish("1.2.3") == {ADDRESS: "published"}

    row = _row(settings, "1.2.3")
    assert row["status"] == "published"
    assert row["sha"] == SHA
    assert row["tag"] == "v1.2.3"
    assert row["error"] is None
    assert _members(settings, "1.2.3") == ["main.tf", "modules/child/main.tf"]
    assert any(f"/tarball/{SHA}" in request.url.path for request in github.requests)


def test_a_bare_version_tag_publishes_too(module, publish, settings):
    """`1.2.3` is as good a tag as `v1.2.3`."""
    assert publish("1.2.3", prefix="") == {ADDRESS: "published"}
    assert _row(settings, "1.2.3")["tag"] == "1.2.3"


def test_a_redelivery_changes_nothing(module, publish, github, settings):
    """A published version is immutable, so the second delivery fetches nothing."""
    publish("1.2.3")
    published_at = _row(settings, "1.2.3")["published_at"]

    assert publish("1.2.3", delivery="d-2") == {ADDRESS: tags.SKIPPED}
    assert publish("1.2.3", sha="f" * 40, delivery="d-3") == {ADDRESS: tags.SKIPPED}
    assert github.tarball_fetches() == 1
    assert _row(settings, "1.2.3")["published_at"] == published_at
    assert _row(settings, "1.2.3")["sha"] == SHA


def test_an_unconnected_repository_publishes_nothing(github, publish):
    """A tag on a repository no module is connected to is acknowledged."""
    assert publish("1.2.3") == {}
    assert github.tarball_fetches() == 0


def test_a_repository_with_no_root_terraform_fails_the_version(module, publish, github, settings):
    """The failure is recorded rather than retried, since the commit cannot change."""
    github.archive = github_tarball({"README.md": "hello", "modules/child/main.tf": ""})

    assert publish("1.2.3") == {ADDRESS: "failed"}

    row = _row(settings, "1.2.3")
    assert row["status"] == "failed"
    assert "no .tf file" in row["error"]


def test_an_archive_escaping_its_root_fails_the_version(module, publish, github, settings):
    """A member naming `..` never reaches the bucket."""

    def escape(archive: tarfile.TarFile) -> None:
        """Add a member climbing out of the module."""
        info = tarfile.TarInfo("WebbPulse-terraform-aws-example-ddddddd/../evil.tf")
        archive.addfile(info, io.BytesIO(b""))

    github.archive = github_tarball(extra=escape)

    assert publish("1.2.3") == {ADDRESS: "failed"}
    assert "escapes" in _row(settings, "1.2.3")["error"]


def test_a_garbled_archive_fails_the_version(module, publish, github, settings):
    """Bytes that are not a gzipped tar are rejected."""
    github.archive = b"not a tarball"

    assert publish("1.2.3") == {ADDRESS: "failed"}


def test_links_state_and_git_are_left_out(module, publish, github, settings):
    """Only regular files and directories of the module are served."""

    def extras(archive: tarfile.TarFile) -> None:
        """Add a symlink, a state file and a .git directory."""
        link = tarfile.TarInfo("WebbPulse-terraform-aws-example-ddddddd/link.tf")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
        for name in ("terraform.tfstate", "terraform.tfstate.backup", ".git/config", ".terraform/x"):
            info = tarfile.TarInfo(f"WebbPulse-terraform-aws-example-ddddddd/{name}")
            archive.addfile(info, io.BytesIO(b""))

    github.archive = github_tarball({"main.tf": ""}, extra=extras)

    assert publish("1.2.3") == {ADDRESS: "published"}
    assert _members(settings, "1.2.3") == ["main.tf"]


def test_a_failed_version_can_be_tagged_again(module, publish, github, settings):
    """Retagging a fixed commit replaces a failure."""
    github.archive = github_tarball({"README.md": ""})
    publish("1.2.3")
    github.archive = github_tarball()

    assert publish("1.2.3", sha="f" * 40, delivery="d-2") == {ADDRESS: "published"}
    assert _row(settings, "1.2.3")["sha"] == "f" * 40


def test_github_refusing_the_archive_raises_for_a_retry(module, publish, github, settings):
    """A transient fault leaves the version pending and the message on the queue."""
    github.redirect = "https://evil.example.com/archive.tar.gz"

    with pytest.raises(TarballUnavailable):
        publish("1.2.3")
    assert _row(settings, "1.2.3")["status"] == "pending"

    github.redirect = "https://codeload.github.com/x"
    assert publish("1.2.3") == {ADDRESS: "published"}


def test_every_connected_module_publishes(auth_client, github, publish):
    """Two modules on one repository each get the version."""
    for body in (
        {"vcs_repo": "WebbPulse/terraform-aws-example"},
        {"vcs_repo": "WebbPulse/terraform-aws-example", "name": "other", "provider": "null"},
    ):
        assert auth_client.post("/api/v1/registry/modules", json=body).status_code == 201

    assert publish("1.0.0") == {ADDRESS: "published", "WebbPulse/other/null": "published"}


def test_no_app_publishes_nothing(module, publish, monkeypatch):
    """A module connected before the App went away is left alone."""
    from app.common.composition import settings as settings_module
    from app.common.github import loader

    monkeypatch.delenv("GITHUB_APP_ID")
    settings_module.reset_settings_cache()
    loader.invalidate()

    assert publish("1.2.3") == {}


def test_dispatch_routes_the_tag_message(module, github, settings):
    """The consumer route hands a `module_tag` body to the tag consumer."""
    dispatch.route_record(tag_record("3.0.0"), settings=settings)
    assert _row(settings, "3.0.0")["status"] == "published"


def test_dispatch_refuses_an_unknown_kind(settings):
    """The retired upload message parks on the dead letter queue."""
    with pytest.raises(dispatch.UnknownRecordKind):
        dispatch.route_record({"body": json.dumps({"kind": "module_ingested"})}, settings=settings)


def test_a_message_of_another_kind_is_malformed(settings):
    """Anything but a tag message is refused."""
    with pytest.raises(MalformedDelivery):
        tags.handle_record({"body": json.dumps({"kind": "module_ingested"})}, settings=settings)
