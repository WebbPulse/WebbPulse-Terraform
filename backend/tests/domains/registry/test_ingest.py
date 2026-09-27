"""The ingest consumer: validating a tarball and publishing or failing its version."""

from __future__ import annotations

import io
import json
import tarfile

import boto3
import pytest

from app.common.db import repositories
from app.domains.registry import service
from app.domains.registry.consumers import dispatch
from app.domains.registry.consumers import ingest as consumer
from tests.domains.registry.conftest import claims, tarball


def _row(settings, version: str = "1.2.3") -> dict:
    """The version row for the example module."""
    key = {"pk": service.module_pk("WebbPulse", "example", "aws"), "sk": service.version_sk(version)}
    return repositories.registry(settings).get(key, consistent=True) or {}


def test_valid_tarball_is_published_to_its_module_key(upload, ingest, settings):
    """The object is copied to the permanent key and the row goes published."""
    upload_id = upload(claims()).json()["upload_id"]

    assert ingest(upload_id, tarball()) == "published"

    row = _row(settings)
    assert row["status"] == "published"
    assert row["key"] == "registry/modules/webbpulse/example/aws/1.2.3.tar.gz"
    s3 = boto3.client("s3", region_name="us-west-2")
    assert s3.head_object(Bucket=settings.ARTIFACTS_BUCKET, Key=row["key"])["ContentType"] == "application/gzip"


def test_archive_without_terraform_fails_the_version(upload, ingest, settings):
    """A tarball with no `.tf` file is not a module."""
    upload_id = upload(claims()).json()["upload_id"]

    assert ingest(upload_id, tarball({"README.md": "hi"})) == "failed"

    row = _row(settings)
    assert row["status"] == "failed"
    assert "no .tf file" in row["error"]


def test_bytes_that_are_not_a_tarball_fail_the_version(upload, ingest, settings):
    """Garbage is rejected with a reason rather than retried."""
    upload_id = upload(claims()).json()["upload_id"]

    assert ingest(upload_id, b"not a tarball") == "failed"
    assert "gzipped tar" in _row(settings)["error"]


def test_failed_version_can_be_retried_and_published(upload, ingest, settings):
    """A failed version is not immutable; the next attempt publishes it."""
    first = upload(claims()).json()["upload_id"]
    ingest(first, b"not a tarball")

    second = upload(claims(run_attempt="2")).json()["upload_id"]

    assert ingest(second, tarball()) == "published"
    assert "error" not in _row(settings)


def test_stale_upload_is_skipped(upload, ingest, settings):
    """An upload superseded by a newer attempt does not touch the version."""
    stale = upload(claims()).json()["upload_id"]
    upload(claims(run_attempt="2"))

    assert ingest(stale, tarball()) == "skipped"
    assert _row(settings)["status"] == "pending"


def test_redelivery_after_publish_is_skipped(upload, ingest):
    """At least once delivery leaves a published version alone."""
    upload_id = upload(claims()).json()["upload_id"]
    ingest(upload_id, tarball())

    assert ingest(upload_id, tarball()) == "skipped"


def test_key_without_an_upload_record_is_skipped(ingest):
    """An object nobody was issued a URL for is dropped."""
    assert ingest("up-" + "0" * 26, tarball()) == "skipped"


def test_key_outside_the_upload_prefix_is_skipped(settings):
    """Only `registry/incoming/<upload id>.tar.gz` is read."""
    body = {"kind": consumer.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": "state/x.tfstate", "size": 1}

    assert consumer.handle_record({"body": json.dumps(body)}, settings=settings) == "skipped"


def test_other_bucket_is_skipped(settings):
    """An event from another bucket is never trusted."""
    body = {
        "kind": consumer.INGEST_KIND,
        "bucket": "elsewhere",
        "key": service.incoming_key("up-" + "0" * 26),
        "size": 1,
    }

    assert consumer.handle_record({"body": json.dumps(body)}, settings=settings) == "skipped"


def test_path_escaping_member_is_rejected():
    """A member reaching outside the module root fails validation."""

    def escape(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("../evil.tf")
        info.size = 0
        archive.addfile(info, io.BytesIO())

    with pytest.raises(consumer.InvalidModuleArchive, match="escapes"):
        consumer.validate_archive(io.BytesIO(tarball(extra=escape)))


def test_symlink_member_is_rejected():
    """Links could point outside the root once unpacked."""

    def link(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("./link.tf")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)

    with pytest.raises(consumer.InvalidModuleArchive, match="not a regular file"):
        consumer.validate_archive(io.BytesIO(tarball(extra=link)))


@pytest.mark.parametrize(
    "body",
    ["", "not json", json.dumps({"kind": "other"}), json.dumps({"kind": consumer.INGEST_KIND, "bucket": "b"})],
)
def test_malformed_body_raises(body):
    """A body the consumer cannot read is parked rather than acknowledged."""
    with pytest.raises(consumer.MalformedIngest):
        consumer.parse_body({"body": body})


def test_dispatch_refuses_an_unknown_kind(settings):
    """Only the kinds this domain handles are routed."""
    with pytest.raises(dispatch.UnknownRecordKind):
        dispatch.route_record({"body": json.dumps({"kind": "run_confirmation"})}, settings=settings)
