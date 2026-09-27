"""The ingest consumer: checks an uploaded module tarball and publishes it.

An EventBridge rule on the artifacts bucket's `Object Created` events under
`registry/incoming/` stamps `module_ingested` on the message. The key names an
upload id, and the module and version come from the upload row the upload route
wrote under that id from a verified GitHub token, never from the object.

A tarball that fails a check marks its version `failed` with the reason and is
acknowledged, since retrying cannot change the bytes. A fault reading S3 or
DynamoDB raises, so the message is retried and eventually parked. Delivery is at
least once: a version already published, or now owned by a newer upload, is left
alone.
"""

from __future__ import annotations

import json
import logging
import posixpath
import re
import tarfile
from typing import Any, Final, Mapping

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed, now_iso

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from .. import service
from ..schemas.registry import MAX_MODULE_BYTES

_log = logging.getLogger(__name__)

INGEST_KIND: Final = "module_ingested"
"""The `kind` the EventBridge rule's input transformer stamps on the message."""

MAX_MEMBERS: Final = 10_000
MAX_UNPACKED_BYTES: Final = 500_000_000

_INCOMING_KEY = re.compile(rf"^registry/incoming/(?P<upload_id>{service.UPLOAD_ID_PATTERN})\.tar\.gz$")


class MalformedIngest(Exception):
    """The message body is not an object created event this consumer can read."""


class InvalidModuleArchive(Exception):
    """The tarball is not a module the registry will serve."""


def parse_body(record: Mapping[str, Any]) -> tuple[str, str, int]:
    """The bucket, key and size one queue record carries.

    Raises:
        MalformedIngest: The body is not JSON, not a `module_ingested`, or lacks a
            bucket, key or size, so it parks on the dead letter queue.
    """
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise MalformedIngest("The record carries no body.")
    try:
        body = json.loads(raw)
    except ValueError as error:
        raise MalformedIngest("The body is not JSON.") from error
    if not isinstance(body, Mapping) or body.get("kind") != INGEST_KIND:
        raise MalformedIngest(f"The body is not a {INGEST_KIND}.")
    bucket, key, size = body.get("bucket"), body.get("key"), body.get("size")
    if not isinstance(bucket, str) or not isinstance(key, str) or not isinstance(size, int):
        raise MalformedIngest("The body lacks a bucket, a key or a size.")
    return bucket, key, size


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def _safe_member_name(name: str) -> bool:
    """Whether a member's path stays inside the directory it unpacks into."""
    stripped = name.removeprefix("./")
    if not stripped or stripped.startswith("/") or "\\" in stripped:
        return False
    normalised = posixpath.normpath(stripped)
    return normalised != ".." and not normalised.startswith("../")


def validate_archive(fileobj: Any) -> None:
    """Check a streamed tarball is a gzipped tar of plain files holding Terraform.

    Only regular files and directories, every path relative and inside the root,
    bounded in member count and unpacked size, and at least one `.tf` file.

    Raises:
        InvalidModuleArchive: The first check that failed.
    """
    members = 0
    unpacked = 0
    terraform_files = 0
    try:
        with tarfile.open(fileobj=fileobj, mode="r|gz") as archive:
            for member in archive:
                members += 1
                if members > MAX_MEMBERS:
                    raise InvalidModuleArchive(f"the archive holds more than {MAX_MEMBERS} entries")
                if not _safe_member_name(member.name):
                    raise InvalidModuleArchive(f"{member.name} escapes the module root")
                if not (member.isfile() or member.isdir()):
                    raise InvalidModuleArchive(f"{member.name} is not a regular file or a directory")
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise InvalidModuleArchive(f"the archive unpacks to more than {MAX_UNPACKED_BYTES} bytes")
                if member.isfile() and member.name.endswith(".tf"):
                    terraform_files += 1
    except (tarfile.TarError, EOFError, OSError) as error:
        raise InvalidModuleArchive(f"the upload is not a gzipped tar archive: {error}") from error
    if terraform_files == 0:
        raise InvalidModuleArchive("the archive holds no .tf file")


def _fail(row_key: Mapping[str, str], upload_id: str, reason: str, *, settings: Settings) -> None:
    """Mark the version `failed`, unless a newer upload now owns it."""
    try:
        repositories.registry(settings).update(
            row_key,
            update_expression="SET #status = :failed, #error = :error, failed_at = :now",
            expression_names={"#status": "status", "#error": "error"},
            expression_values={":failed": service.FAILED, ":error": reason, ":now": now_iso()},
            condition=Attr("upload_id").eq(upload_id) & Attr("status").ne(service.PUBLISHED),
        )
    except ConditionFailed:
        return


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> str:
    """Check one uploaded tarball and publish its version, returning the outcome.

    The outcome is `published`, `failed` or `skipped`; a skip is a key with no
    upload row, or a version no longer waiting on this upload.
    """
    resolved = settings or get_settings()
    bucket, key, size = parse_body(record)
    match = _INCOMING_KEY.match(key)
    if match is None or bucket != resolved.ARTIFACTS_BUCKET:
        _log.warning(
            "Dropped an object outside the upload keys.", extra={"event": "registry.ingest.unknown_key", "key": key}
        )
        return "skipped"
    upload_id = match.group("upload_id")
    upload = service.upload_record(upload_id, settings=resolved)
    if upload is None:
        _log.warning(
            "Dropped an upload with no record.", extra={"event": "registry.ingest.no_record", "upload_id": upload_id}
        )
        return "skipped"

    namespace, name, provider = str(upload["namespace"]), str(upload["name"]), str(upload["provider"])
    version = str(upload["version"])
    row_key = {"pk": service.module_pk(namespace, name, provider), "sk": service.version_sk(version)}
    row = repositories.registry(resolved).get(row_key, consistent=True)
    if not row or row.get("upload_id") != upload_id or row.get("status") == service.PUBLISHED:
        _log.info(
            "Skipped an upload its version no longer waits on.",
            extra={"event": "registry.ingest.stale", "upload_id": upload_id},
        )
        return "skipped"

    if size > MAX_MODULE_BYTES:
        _fail(row_key, upload_id, f"the archive is larger than {MAX_MODULE_BYTES} bytes", settings=resolved)
        return service.FAILED

    s3 = _s3(resolved)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        validate_archive(body)
    except InvalidModuleArchive as error:
        _fail(row_key, upload_id, str(error), settings=resolved)
        _log.info(
            "Rejected a module upload.",
            extra={"event": "registry.ingest.rejected", "upload_id": upload_id, "reason": str(error)},
        )
        return service.FAILED
    finally:
        body.close()

    destination = service.module_key(namespace, name, provider, version)
    s3.copy_object(
        Bucket=resolved.ARTIFACTS_BUCKET,
        Key=destination,
        CopySource={"Bucket": bucket, "Key": key},
        ContentType=service.UPLOAD_CONTENT_TYPE,
        MetadataDirective="REPLACE",
    )
    try:
        repositories.registry(resolved).update(
            row_key,
            update_expression=(
                "SET #status = :published, #key = :key, size_bytes = :size, published_at = :now REMOVE #error"
            ),
            expression_names={"#status": "status", "#key": "key", "#error": "error"},
            expression_values={":published": service.PUBLISHED, ":key": destination, ":size": size, ":now": now_iso()},
            condition=Attr("upload_id").eq(upload_id) & Attr("status").ne(service.PUBLISHED),
        )
    except ConditionFailed:
        return "skipped"
    _log.info(
        "Published a module version.",
        extra={
            "event": "registry.ingest.published",
            "upload_id": upload_id,
            "module_address": f"{namespace}/{name}/{provider}",
            "version": version,
        },
    )
    return service.PUBLISHED


__all__ = [
    "INGEST_KIND",
    "MAX_MEMBERS",
    "MAX_UNPACKED_BYTES",
    "InvalidModuleArchive",
    "MalformedIngest",
    "handle_record",
    "parse_body",
    "validate_archive",
]
