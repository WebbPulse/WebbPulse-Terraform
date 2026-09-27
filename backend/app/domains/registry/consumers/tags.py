"""The tag consumer: a semantic version tag push publishing that version of each connected module.

The webhook route queues a `module_tag` message for a push of `vX.Y.Z` or `X.Y.Z`.
For every module connected to the repository, the version row is claimed
`pending`, the archive at the tag's commit is read through the App's installation
token, repacked with the module at its root, stored under `registry/modules/` and
the row moved to `published`.

A tag sync queues the same message for each tag it imports, carrying a `module`
partition key so only that module publishes it; everything past that is shared,
so a tag the webhook and a sync both deliver publishes once.

Delivery is at least once and GitHub may redeliver. A version already published
is left alone, so a redelivery, or the same version tagged again, changes
nothing. An archive that fails a check marks the version `failed` and is
acknowledged, since retrying cannot change a commit's bytes; a fault reaching
GitHub, S3 or DynamoDB raises so the message is retried and eventually parked.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Final, Mapping

import httpx
from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed, now_iso
from webbpulse.integrations.github import GitHubAppClient, GitHubNotConfigured

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from ....common.github.archive import download_tarball
from ....common.github.loader import github_app_settings
from ....common.github.webhooks import TAG_KIND, parse_tag_message
from .. import service
from ..archive import InvalidModuleArchive, repack
from ..schemas.registry import MAX_MODULE_BYTES

_log = logging.getLogger(__name__)

KIND: Final = TAG_KIND
READ_TIMEOUT_SECONDS: Final = 30.0
MAX_ARCHIVE_BYTES: Final = 200_000_000
"""The largest repository archive fetched, so the download and the repack both fit
in the function's temporary storage."""

SKIPPED: Final = "skipped"


def http_client() -> httpx.Client:
    """The HTTP client GitHub calls go through. The seam the tests replace."""
    return httpx.Client(timeout=READ_TIMEOUT_SECONDS, follow_redirects=False)


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def _claim(module: Mapping[str, Any], message: Mapping[str, Any], *, settings: Settings) -> bool:
    """Write the version row `pending` for this tag, unless the version is already published."""
    namespace, name, provider = str(module["namespace"]), str(module["name"]), str(module["provider"])
    try:
        repositories.registry(settings).put(
            {
                "pk": service.module_pk(namespace, name, provider),
                "sk": service.version_sk(str(message["version"])),
                "namespace": namespace,
                "name": name,
                "provider": provider,
                "version": str(message["version"]),
                "status": service.PENDING,
                "repository": str(message["repo"]),
                "tag": str(message["tag"]),
                "sha": str(message["sha"]),
                "actor": str(message["actor"]),
                "delivery": str(message["delivery"]),
                "created_at": now_iso(),
            },
            condition=Attr("pk").not_exists() | Attr("status").ne(service.PUBLISHED),
        )
    except ConditionFailed:
        return False
    return True


def _settle(row_key: Mapping[str, str], sha: str, values: Mapping[str, Any], *, settings: Settings) -> bool:
    """Move a claimed version on, unless it was published meanwhile or reclaimed for another commit."""
    names = {"#status": "status"}
    assignments = ["#status = :status"]
    expression_values: dict[str, Any] = {":status": values["status"]}
    for field, value in values.items():
        if field == "status":
            continue
        names[f"#{field}"] = field
        assignments.append(f"#{field} = :{field}")
        expression_values[f":{field}"] = value
    try:
        repositories.registry(settings).update(
            row_key,
            update_expression="SET " + ", ".join(assignments),
            expression_names=names,
            expression_values=expression_values,
            condition=Attr("sha").eq(sha) & Attr("status").ne(service.PUBLISHED),
        )
    except ConditionFailed:
        return False
    return True


def publish(
    module: Mapping[str, Any],
    message: Mapping[str, Any],
    app: GitHubAppClient,
    http: httpx.Client,
    *,
    settings: Settings,
) -> str:
    """Publish the tagged version of one module, returning `published`, `failed` or `skipped`."""
    namespace, name, provider = str(module["namespace"]), str(module["name"]), str(module["provider"])
    version, sha = str(message["version"]), str(message["sha"])
    address = f"{namespace}/{name}/{provider}"
    extra = {"module_address": address, "version": version, "delivery": message["delivery"]}
    if not _claim(module, message, settings=settings):
        _log.info("Skipped a version already published.", extra={"event": "registry.tag.published_already", **extra})
        return SKIPPED
    row_key = {"pk": service.module_pk(namespace, name, provider), "sk": service.version_sk(version)}
    with tempfile.TemporaryDirectory() as scratch:
        downloaded = Path(scratch) / "github.tar.gz"
        packed = Path(scratch) / "module.tar.gz"
        download_tarball(
            app,
            http,
            installation_id=str(message["installation_id"]),
            repository=str(message["repo"]),
            ref=sha,
            target=downloaded,
            max_bytes=MAX_ARCHIVE_BYTES,
        )
        try:
            repack(downloaded, packed)
            size = packed.stat().st_size
            if size > MAX_MODULE_BYTES:
                raise InvalidModuleArchive(f"the module is larger than {MAX_MODULE_BYTES} bytes")
        except InvalidModuleArchive as error:
            _settle(
                row_key, sha, {"status": service.FAILED, "error": str(error), "failed_at": now_iso()}, settings=settings
            )
            _log.info(
                "Rejected a tagged module version.",
                extra={"event": "registry.tag.rejected", "reason": str(error), **extra},
            )
            return service.FAILED
        destination = service.module_key(namespace, name, provider, version)
        _s3(settings).upload_file(
            str(packed),
            settings.ARTIFACTS_BUCKET,
            destination,
            ExtraArgs={"ContentType": service.MODULE_CONTENT_TYPE},
        )
    settled = _settle(
        row_key,
        sha,
        {"status": service.PUBLISHED, "key": destination, "size_bytes": size, "published_at": now_iso(), "error": None},
        settings=settings,
    )
    if not settled:
        return SKIPPED
    _log.info("Published a module version from a tag.", extra={"event": "registry.tag.published", **extra})
    return service.PUBLISHED


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> dict[str, str]:
    """Publish the tagged version of every module connected to the repository, or only the one it names.

    Returns each module address against its outcome; a repository with no
    connected module, or an environment with no App, publishes nothing.

    Raises:
        MalformedDelivery: The body is not a `module_tag` message.
        TarballUnavailable: GitHub did not hand over the archive, so SQS retries.
    """
    resolved = settings or get_settings()
    message = parse_tag_message(record)
    modules = service.connected_modules(str(message["repository_id"]), settings=resolved)
    if message.get("module"):
        modules = [row for row in modules if str(row["pk"]) == str(message["module"])]
    extra = {"delivery": message["delivery"], "repository": message["repo"], "tag": message["tag"]}
    if not modules:
        _log.info(
            "No module is connected to the tagged repository.", extra={"event": "registry.tag.unconnected", **extra}
        )
        return {}
    try:
        credentials = github_app_settings(resolved.app_secret_arn, region_name=resolved.AWS_REGION_NAME)
    except GitHubNotConfigured:
        _log.warning("No GitHub App to read the tag through.", extra={"event": "registry.tag.no_app", **extra})
        return {}
    outcomes: dict[str, str] = {}
    with http_client() as http, GitHubAppClient.from_settings(credentials, client=http) as app:
        for module in sorted(modules, key=lambda row: str(row["pk"])):
            address = f"{module['namespace']}/{module['name']}/{module['provider']}"
            outcomes[address] = publish(module, message, app, http, settings=resolved)
    return outcomes


__all__ = ["KIND", "MAX_ARCHIVE_BYTES", "SKIPPED", "handle_record", "http_client", "publish"]
