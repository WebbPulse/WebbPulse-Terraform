"""The provider consumers: a published GitHub release publishing that version of each connected provider.

The webhook route queues a `provider_release` message when a release with a
semantic version tag is published. For every provider connected to the
repository, the version row is claimed `pending`, the release is read by its tag
through the App's installation token, the signature over its `SHA256SUMS` is
checked against this environment's signing key, each platform zip is fetched,
hashed against that list and stored under `registry/providers/`, and the row moves
to `published`.

Connecting a provider and resyncing one queue a `provider_sync` message instead,
which lists the repository's releases and queues one `provider_release` per
version not yet published, scoped to that provider, so each release is fetched in
its own invocation.

A release that fails a check, whether a missing file, a bad signature or a
mismatched hash, marks the version `failed` and is acknowledged; a later resync
or a new release retries it. A fault reaching GitHub, S3, SSM or DynamoDB raises
so SQS retries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Final, Mapping

import httpx
from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed, now_iso
from webbpulse.integrations.github import GitHubAppClient, GitHubNotConfigured

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from ....common.github.loader import github_app_settings
from ....common.github.releases import ReleaseNotFound, download_asset, get_release, list_releases
from ....common.github.webhooks import RELEASE_KIND, MalformedDelivery, parse_release_message, semver_version
from .. import providers, service
from ..openpgp import SignatureInvalid, verify_detached
from . import tags

_log = logging.getLogger(__name__)

RELEASE: Final = RELEASE_KIND
SYNC: Final = providers.SYNC_KIND
SKIPPED: Final = "skipped"
MAX_RELEASE_PAGES: Final = 5
"""Pages of 100 releases read per sync, newest first by GitHub's ordering."""
MAX_SYNC_VERSIONS: Final = 50
"""The most versions one sync queues, the newest by semantic order."""
MAX_SMALL_ASSET_BYTES: Final = 1_000_000
"""The largest checksum list, signature or manifest fetched."""
MAX_ZIP_BYTES: Final = 200_000_000
"""The largest platform zip fetched, well inside the function's temporary storage."""
BATCH_SIZE: Final = 10

_SUMS_LINE = re.compile(r"^(?P<hash>[0-9a-f]{64}) [ *](?P<name>\S+)$")
_PLATFORM = re.compile(r"^(?P<os>[a-z0-9]+)_(?P<arch>[a-z0-9]+)\.zip$")


class InvalidRelease(Exception):
    """The release does not carry a verifiable provider build, so retrying cannot help."""


def _ssm(settings: Settings) -> Any:
    """An SSM client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("ssm", region_name=settings.AWS_REGION_NAME or None)


def signing_key(settings: Settings) -> tuple[str, str]:
    """The armored public key releases must be signed with, and its id, from this environment's SSM parameters.

    Raises:
        InvalidRelease: No signing key parameter is configured here.
    """
    if not settings.PROVIDER_SIGNING_KEY_PARAMETER:
        raise InvalidRelease("no provider signing key is configured in this environment")
    client = _ssm(settings)
    armored = str(client.get_parameter(Name=settings.PROVIDER_SIGNING_KEY_PARAMETER)["Parameter"]["Value"])
    key_id = ""
    if settings.PROVIDER_SIGNING_KEY_ID_PARAMETER:
        key_id = str(client.get_parameter(Name=settings.PROVIDER_SIGNING_KEY_ID_PARAMETER)["Parameter"]["Value"])
    return armored, key_id.strip().upper()


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def parse_sums(text: str) -> dict[str, str]:
    """Each file a `SHA256SUMS` list names, against its hex digest.

    Raises:
        InvalidRelease: A line is not `<sha256>  <filename>`.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _SUMS_LINE.match(line.strip())
        if match is None:
            raise InvalidRelease("the SHA256SUMS file is malformed")
        found[match.group("name")] = match.group("hash")
    return found


def _claim(provider: Mapping[str, Any], message: Mapping[str, Any], *, settings: Settings) -> bool:
    """Write the version row `pending` for this release, unless the version is already published."""
    namespace, type_ = str(provider["namespace"]), str(provider["type"])
    try:
        repositories.registry(settings).put(
            {
                "pk": providers.provider_pk(namespace, type_),
                "sk": service.version_sk(str(message["version"])),
                "namespace": namespace,
                "type": type_,
                "version": str(message["version"]),
                "status": service.PENDING,
                "repository": str(message["repo"]),
                "tag": str(message["tag"]),
                "actor": str(message["actor"]),
                "delivery": str(message["delivery"]),
                "created_at": now_iso(),
            },
            condition=Attr("pk").not_exists() | Attr("status").ne(service.PUBLISHED),
        )
    except ConditionFailed:
        return False
    return True


def _settle(row_key: Mapping[str, str], delivery: str, values: Mapping[str, Any], *, settings: Settings) -> bool:
    """Move a claimed version on, unless it was published meanwhile or claimed again by another delivery."""
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
            condition=Attr("delivery").eq(delivery) & Attr("status").ne(service.PUBLISHED),
        )
    except ConditionFailed:
        return False
    return True


def _asset(assets: Mapping[str, Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    """The release asset of that name.

    Raises:
        InvalidRelease: The release does not carry it.
    """
    found = assets.get(name)
    if found is None:
        raise InvalidRelease(f"the release has no {name}")
    return found


def _fetch_small(
    app: GitHubAppClient, http: httpx.Client, asset: Mapping[str, Any], target: Path, message: Mapping[str, Any]
) -> bytes:
    """One small asset's bytes, fetched into `target`."""
    download_asset(
        app,
        http,
        installation_id=str(message["installation_id"]),
        repository=str(message["repo"]),
        asset_id=str(asset["id"]),
        target=target,
        max_bytes=MAX_SMALL_ASSET_BYTES,
    )
    return target.read_bytes()


def _protocols(manifest: bytes | None) -> list[str]:
    """The plugin protocol versions a release's manifest names, or Terraform's default without one.

    Raises:
        InvalidRelease: The manifest is not the registry manifest shape.
    """
    if manifest is None:
        return list(providers.DEFAULT_PROTOCOLS)
    try:
        body = json.loads(manifest)
        listed = body["metadata"]["protocol_versions"]
    except (ValueError, KeyError, TypeError) as error:
        raise InvalidRelease("the manifest does not name protocol_versions") from error
    if not isinstance(listed, list) or not listed or not all(isinstance(item, str) for item in listed):
        raise InvalidRelease("the manifest does not name protocol_versions")
    return [str(item) for item in listed]


def _ingest(
    provider: Mapping[str, Any],
    message: Mapping[str, Any],
    release: Mapping[str, Any],
    app: GitHubAppClient,
    http: httpx.Client,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Verify and store one release's files, returning the fields a published row carries.

    Raises:
        InvalidRelease: The release is not a verifiable provider build.
        ReleaseUnavailable: GitHub did not hand over an asset.
    """
    namespace, type_ = str(provider["namespace"]), str(provider["type"])
    version = str(message["version"])
    assets = {str(item.get("name")): item for item in release.get("assets") or [] if isinstance(item, Mapping)}
    sums_names = [name for name in assets if name.endswith(f"_{version}_SHA256SUMS")]
    if len(sums_names) != 1:
        raise InvalidRelease(f"the release has no single *_{version}_SHA256SUMS file")
    sums_name = sums_names[0]
    stem = sums_name[: -len("SHA256SUMS")]
    signature_name = f"{sums_name}.sig"
    manifest_name = f"{stem}manifest.json"
    armored, expected_key_id = signing_key(settings)
    s3 = _s3(settings)
    bucket = settings.ARTIFACTS_BUCKET
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch)
        sums = _fetch_small(app, http, _asset(assets, sums_name), folder / "sums", message)
        signature = _fetch_small(app, http, _asset(assets, signature_name), folder / "sig", message)
        try:
            key_id = verify_detached(sums, signature, armored)
        except SignatureInvalid as error:
            raise InvalidRelease(f"the SHA256SUMS signature is not valid: {error}") from error
        if expected_key_id and not expected_key_id.endswith(key_id):
            raise InvalidRelease(f"the SHA256SUMS file is signed by {key_id}, not the registry's signing key")
        try:
            listed = parse_sums(sums.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise InvalidRelease("the SHA256SUMS file is not text") from error
        manifest: bytes | None = None
        if manifest_name in assets:
            manifest = _fetch_small(app, http, assets[manifest_name], folder / "manifest", message)
            expected = listed.get(manifest_name)
            if expected is not None and hashlib.sha256(manifest).hexdigest() != expected:
                raise InvalidRelease("the manifest does not match its SHA256SUMS entry")
        protocols = _protocols(manifest)
        platforms: list[dict[str, str]] = []
        for filename in sorted(listed):
            if not filename.startswith(stem):
                continue
            match = _PLATFORM.match(filename[len(stem) :])
            if match is None:
                continue
            target = folder / "build.zip"
            digest = download_asset(
                app,
                http,
                installation_id=str(message["installation_id"]),
                repository=str(message["repo"]),
                asset_id=str(_asset(assets, filename)["id"]),
                target=target,
                max_bytes=MAX_ZIP_BYTES,
            )
            if digest != listed[filename]:
                raise InvalidRelease(f"{filename} does not match its SHA256SUMS entry")
            key = providers.artifact_key(namespace, type_, version, filename)
            s3.upload_file(str(target), bucket, key, ExtraArgs={"ContentType": "application/zip"})
            target.unlink()
            platforms.append(
                {
                    "os": match.group("os"),
                    "arch": match.group("arch"),
                    "filename": filename,
                    "shasum": digest,
                    "key": key,
                }
            )
        if not platforms:
            raise InvalidRelease("the SHA256SUMS file lists no platform zip")
        shasums_key = providers.artifact_key(namespace, type_, version, sums_name)
        signature_key = providers.artifact_key(namespace, type_, version, signature_name)
        s3.put_object(Bucket=bucket, Key=shasums_key, Body=sums, ContentType="text/plain")
        s3.put_object(Bucket=bucket, Key=signature_key, Body=signature, ContentType="application/octet-stream")
    return {
        "protocols": protocols,
        "platforms": platforms,
        "shasums_key": shasums_key,
        "signature_key": signature_key,
        "key_id": key_id,
        "ascii_armor": armored,
    }


def publish(
    provider: Mapping[str, Any],
    message: Mapping[str, Any],
    app: GitHubAppClient,
    http: httpx.Client,
    *,
    settings: Settings,
) -> str:
    """Publish the released version of one provider, returning `published`, `failed` or `skipped`."""
    namespace, type_ = str(provider["namespace"]), str(provider["type"])
    version, delivery = str(message["version"]), str(message["delivery"])
    extra = {"provider_address": f"{namespace}/{type_}", "version": version, "delivery": delivery}
    if not _claim(provider, message, settings=settings):
        _log.info(
            "Skipped a provider version already published.",
            extra={"event": "registry.release.published_already", **extra},
        )
        return SKIPPED
    row_key = {"pk": providers.provider_pk(namespace, type_), "sk": service.version_sk(version)}
    try:
        release = get_release(
            app,
            http,
            installation_id=str(message["installation_id"]),
            repository=str(message["repo"]),
            tag=str(message["tag"]),
        )
        values = _ingest(provider, message, release, app, http, settings=settings)
    except (InvalidRelease, ReleaseNotFound) as error:
        reason = str(error) if isinstance(error, InvalidRelease) else f"no published release at {message['tag']}"
        _settle(
            row_key, delivery, {"status": service.FAILED, "error": reason, "failed_at": now_iso()}, settings=settings
        )
        _log.info(
            "Rejected a provider release.", extra={"event": "registry.release.rejected", "reason": reason, **extra}
        )
        return service.FAILED
    settled = _settle(
        row_key,
        delivery,
        {**values, "status": service.PUBLISHED, "published_at": now_iso(), "error": None},
        settings=settings,
    )
    if not settled:
        return SKIPPED
    _log.info("Published a provider version from a release.", extra={"event": "registry.release.published", **extra})
    return service.PUBLISHED


def _app_credentials(settings: Settings, extra: Mapping[str, Any]) -> Any:
    """The App's credentials, or `None` when this environment has no App."""
    try:
        return github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    except GitHubNotConfigured:
        _log.warning("No GitHub App to read releases through.", extra={"event": "registry.release.no_app", **extra})
        return None


def handle_release(record: Mapping[str, Any], *, settings: Settings | None = None) -> dict[str, str]:
    """Publish the released version of every provider connected to the repository, or only the one it names.

    Raises:
        MalformedDelivery: The body is not a `provider_release` message.
        ReleaseUnavailable: GitHub did not answer, so SQS retries.
    """
    resolved = settings or get_settings()
    message = parse_release_message(record)
    connected = providers.connected_providers(str(message["repository_id"]), settings=resolved)
    if message.get("provider"):
        connected = [row for row in connected if str(row["pk"]) == str(message["provider"])]
    extra = {"delivery": message["delivery"], "repository": message["repo"], "tag": message["tag"]}
    if not connected:
        _log.info(
            "No provider is connected to the released repository.",
            extra={"event": "registry.release.unconnected", **extra},
        )
        return {}
    credentials = _app_credentials(resolved, extra)
    if credentials is None:
        return {}
    outcomes: dict[str, str] = {}
    with tags.http_client() as http, GitHubAppClient.from_settings(credentials, client=http) as app:
        for provider in sorted(connected, key=lambda row: str(row["pk"])):
            outcomes[f"{provider['namespace']}/{provider['type']}"] = publish(
                provider, message, app, http, settings=resolved
            )
    return outcomes


def parse_sync_message(record: Mapping[str, Any]) -> dict[str, Any]:
    """The queued provider sync request one SQS record carries.

    Raises:
        MalformedDelivery: The body is not a `provider_sync` message naming a provider.
    """
    raw = record.get("body")
    try:
        body = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise MalformedDelivery("The body is not JSON.") from error
    if not isinstance(body, dict) or body.get("kind") != SYNC:
        raise MalformedDelivery(f"The body is not a {SYNC} message.")
    missing = [name for name in ("delivery", "provider") if not body.get(name)]
    if missing:
        raise MalformedDelivery(f"The message lacks {', '.join(missing)}.")
    return body


def _queue(messages: list[dict[str, Any]], *, settings: Settings) -> None:
    """Send the release messages to the ingest queue in batches, raising if any is refused."""
    sqs = service.sqs_client(settings)
    for start in range(0, len(messages), BATCH_SIZE):
        batch = messages[start : start + BATCH_SIZE]
        response = sqs.send_message_batch(
            QueueUrl=settings.REGISTRY_INGEST_QUEUE_URL,
            Entries=[
                {"Id": str(index), "MessageBody": json.dumps(message, separators=(",", ":"))}
                for index, message in enumerate(batch)
            ],
        )
        if response.get("Failed"):
            raise RuntimeError(f"SQS refused {len(response['Failed'])} release messages")


def handle_sync(record: Mapping[str, Any], *, settings: Settings | None = None) -> dict[str, str]:
    """Queue a publish of each released version the provider has not published, returning version against tag.

    Raises:
        MalformedDelivery: The body is not a `provider_sync` message.
        ReleaseUnavailable: GitHub did not list the releases, so SQS retries.
    """
    resolved = settings or get_settings()
    message = parse_sync_message(record)
    extra = {"delivery": message["delivery"], "provider": message["provider"]}
    provider = repositories.registry(resolved).get({"pk": str(message["provider"]), "sk": providers.PROVIDER_SK})
    if not provider or not provider.get("vcs_repo") or not provider.get("vcs_installation_id"):
        _log.info("No connected provider to sync.", extra={"event": "registry.provider_sync.unconnected", **extra})
        return {}
    if not resolved.REGISTRY_INGEST_QUEUE_URL:
        _log.warning("No ingest queue to sync through.", extra={"event": "registry.provider_sync.no_queue", **extra})
        return {}
    credentials = _app_credentials(resolved, extra)
    if credentials is None:
        return {}
    with tags.http_client() as http, GitHubAppClient.from_settings(credentials, client=http) as app:
        listed = list_releases(
            app,
            http,
            installation_id=str(provider["vcs_installation_id"]),
            repository=str(provider["vcs_repo"]),
            max_pages=MAX_RELEASE_PAGES,
        )
    found: dict[str, str] = {}
    for release in listed:
        if release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        version = semver_version(tag)
        if version is not None and (version not in found or tag.startswith("v")):
            found[version] = tag
    published = {
        str(row["version"])
        for row in providers.version_rows(str(provider["namespace"]), str(provider["type"]), settings=resolved)
        if row.get("status") == service.PUBLISHED
    }
    wanted = [
        version
        for version in sorted(found, key=service.version_order, reverse=True)[:MAX_SYNC_VERSIONS]
        if version not in published
    ]
    messages = [
        {
            "kind": RELEASE_KIND,
            "delivery": f"{message['delivery']}-{version}",
            "event": "sync",
            "repo": str(provider["vcs_repo"]),
            "repository_id": str(provider["vcs_repository_id"]),
            "installation_id": str(provider["vcs_installation_id"]),
            "actor": str(message.get("actor") or "sync"),
            "tag": found[version],
            "version": version,
            "provider": str(message["provider"]),
        }
        for version in wanted
    ]
    _queue(messages, settings=resolved)
    _log.info(
        "Synced a provider's releases.",
        extra={
            "event": "registry.provider_sync.queued",
            "releases_listed": len(listed),
            "versions_found": len(found),
            "versions_queued": len(messages),
            **extra,
        },
    )
    return {version: found[version] for version in wanted}


__all__ = [
    "InvalidRelease",
    "MAX_SYNC_VERSIONS",
    "RELEASE",
    "SKIPPED",
    "SYNC",
    "handle_release",
    "handle_sync",
    "parse_sums",
    "parse_sync_message",
    "publish",
    "signing_key",
]
