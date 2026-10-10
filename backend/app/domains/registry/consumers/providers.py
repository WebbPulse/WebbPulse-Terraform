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

A release workflow can publish without the App instead: its OIDC role uploads the
same files and an `upload.json` under `registry/provider-uploads/`, and EventBridge
queues a `provider_upload` message for that object, which runs the same checks
against the folder and deletes it once settled.

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
from typing import Any, Callable, Collection, Final, Mapping

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


Fetch = Callable[[str, Path, int], str]
"""Fetches one named release file into a path, capped at a size, returning its hex SHA-256."""


def _fetch_small(fetch: Fetch, names: Collection[str], name: str, target: Path) -> bytes:
    """One small release file's bytes.

    Raises:
        InvalidRelease: The release does not carry it.
    """
    if name not in names:
        raise InvalidRelease(f"the release has no {name}")
    fetch(name, target, MAX_SMALL_ASSET_BYTES)
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
    version: str,
    names: Collection[str],
    fetch: Fetch,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Verify and store one release's files, whatever carried them, returning the fields a published row carries.

    Raises:
        InvalidRelease: The release is not a verifiable provider build.
        ReleaseUnavailable: GitHub did not hand over an asset.
    """
    namespace, type_ = str(provider["namespace"]), str(provider["type"])
    sums_names = [name for name in names if name.endswith(f"_{version}_SHA256SUMS")]
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
        sums = _fetch_small(fetch, names, sums_name, folder / "sums")
        signature = _fetch_small(fetch, names, signature_name, folder / "sig")
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
        if manifest_name in names:
            manifest = _fetch_small(fetch, names, manifest_name, folder / "manifest")
            expected = listed.get(manifest_name)
            if expected is None:
                raise InvalidRelease("the manifest is not listed in SHA256SUMS")
            if hashlib.sha256(manifest).hexdigest() != expected:
                raise InvalidRelease("the manifest does not match its SHA256SUMS entry")
        protocols = _protocols(manifest)
        platforms: list[dict[str, str]] = []
        for filename in sorted(listed):
            if not filename.startswith(stem):
                continue
            match = _PLATFORM.match(filename[len(stem) :])
            if match is None:
                continue
            if filename not in names:
                raise InvalidRelease(f"the release has no {filename}")
            target = folder / "build.zip"
            digest = fetch(filename, target, MAX_ZIP_BYTES)
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


Source = Callable[[], tuple[Collection[str], Fetch]]
"""Opens a release's files, returning their names and a fetch for each, raising `ReleaseNotFound` for no release."""


def _publish(provider: Mapping[str, Any], message: Mapping[str, Any], source: Source, *, settings: Settings) -> str:
    """Claim, verify and settle one version of one provider, whatever carried its files.

    Returns `published`, `failed` or `skipped`.
    """
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
        names, fetch = source()
        values = _ingest(provider, version, names, fetch, settings=settings)
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
    _log.info("Published a provider version.", extra={"event": "registry.release.published", **extra})
    return service.PUBLISHED


def publish(
    provider: Mapping[str, Any],
    message: Mapping[str, Any],
    app: GitHubAppClient,
    *,
    settings: Settings,
) -> str:
    """Publish the released version of one provider from its GitHub release assets."""
    installation_id, repository = str(message["installation_id"]), str(message["repo"])

    def source() -> tuple[Collection[str], Fetch]:
        """The release's asset names and a fetch through the installation token."""
        release = get_release(app, installation_id=installation_id, repository=repository, tag=str(message["tag"]))
        assets = {asset.name: asset.id for asset in release.assets}

        def fetch(name: str, target: Path, max_bytes: int) -> str:
            """Download one asset by its id."""
            return download_asset(
                app,
                installation_id=installation_id,
                repository=repository,
                asset_id=assets[name],
                target=target,
                max_bytes=max_bytes,
            )

        return set(assets), fetch

    return _publish(provider, message, source, settings=settings)


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
            outcomes[f"{provider['namespace']}/{provider['type']}"] = publish(provider, message, app, settings=resolved)
    return outcomes


UPLOAD: Final = "provider_upload"
"""The `kind` of an S3 Object Created on a release workflow's `upload.json`, routed by EventBridge."""
UPLOADS_PREFIX: Final = "registry/provider-uploads/"
MAX_UPLOAD_OBJECTS: Final = 64
"""The most files one upload folder may hold, far above a release's platforms."""

_UPLOAD_KEY = re.compile(
    r"^registry/provider-uploads/(?P<namespace>[0-9a-z][0-9a-z_-]{0,63})/(?P<type>[0-9a-z][0-9a-z-]{0,62})/"
    r"(?P<version>[0-9A-Za-z.+-]{1,128})/(?P<upload>[0-9A-Za-z_-]{1,64})/upload\.json$"
)


def parse_upload_message(record: Mapping[str, Any]) -> dict[str, Any]:
    """The bucket and key of the uploaded `upload.json` one SQS record carries.

    Raises:
        MalformedDelivery: The body is not a `provider_upload` message naming a bucket and key.
    """
    raw = record.get("body")
    try:
        body = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise MalformedDelivery("The body is not JSON.") from error
    if not isinstance(body, dict) or body.get("kind") != UPLOAD:
        raise MalformedDelivery(f"The body is not a {UPLOAD} message.")
    missing = [name for name in ("bucket", "key") if not body.get(name)]
    if missing:
        raise MalformedDelivery(f"The message lacks {', '.join(missing)}.")
    return body


def _s3_fetch(s3: Any, bucket: str, folder: str) -> Fetch:
    """A fetch of one file of an upload folder, streamed to disk and hashed, refusing one over its cap."""

    def fetch(name: str, target: Path, max_bytes: int) -> str:
        """Stream one uploaded file into `target`, returning its hex SHA-256.

        Raises:
            InvalidRelease: The file is larger than `max_bytes`.
        """
        response = s3.get_object(Bucket=bucket, Key=folder + name)
        if int(response.get("ContentLength") or 0) > max_bytes:
            response["Body"].close()
            raise InvalidRelease(f"{name} is larger than {max_bytes} bytes")
        digest = hashlib.sha256()
        written = 0
        with target.open("wb") as handle:
            for chunk in response["Body"].iter_chunks(chunk_size=1 << 20):
                written += len(chunk)
                if written > max_bytes:
                    raise InvalidRelease(f"{name} is larger than {max_bytes} bytes")
                digest.update(chunk)
                handle.write(chunk)
        return digest.hexdigest()

    return fetch


def _upload_names(s3: Any, bucket: str, folder: str) -> list[str]:
    """The file names directly inside an upload folder.

    Raises:
        InvalidRelease: The folder holds more files than any release would.
    """
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=folder, MaxKeys=MAX_UPLOAD_OBJECTS + 1)
    names = [str(item["Key"])[len(folder) :] for item in listed.get("Contents") or []]
    if len(names) > MAX_UPLOAD_OBJECTS or listed.get("IsTruncated"):
        raise InvalidRelease(f"the upload holds more than {MAX_UPLOAD_OBJECTS} files")
    return [name for name in names if name and "/" not in name]


def _discard_upload(s3: Any, bucket: str, folder: str) -> None:
    """Delete everything under an upload folder once it has been settled either way."""
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=folder, MaxKeys=1000)
    keys = [{"Key": str(item["Key"])} for item in listed.get("Contents") or []]
    if keys:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})


def _upload_request(s3: Any, bucket: str, key: str, match: re.Match[str]) -> dict[str, str]:
    """The release an upload describes, checked against the folder it sits in.

    Raises:
        InvalidRelease: `upload.json` is not the expected shape or disagrees with its key.
    """
    head = s3.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength") or 0) > MAX_SMALL_ASSET_BYTES:
        raise InvalidRelease("upload.json is too large")
    try:
        body = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except ValueError as error:
        raise InvalidRelease("upload.json is not JSON") from error
    if not isinstance(body, dict):
        raise InvalidRelease("upload.json is not an object")
    fields = {name: body.get(name) for name in ("repository", "tag", "actor", "run_url")}
    if not all(isinstance(fields[name], str) and fields[name] for name in ("repository", "tag")):
        raise InvalidRelease("upload.json does not name a repository and a tag")
    repository, tag = str(fields["repository"]), str(fields["tag"])
    try:
        namespace, type_ = providers.provider_for(repository)
    except providers.InvalidProviderName as error:
        raise InvalidRelease(str(error)) from error
    if (namespace.lower(), type_) != (match.group("namespace"), match.group("type")):
        raise InvalidRelease(f"{repository} does not publish {match.group('namespace')}/{match.group('type')}")
    if semver_version(tag) != match.group("version"):
        raise InvalidRelease(f"{tag} does not publish version {match.group('version')}")
    return {
        "namespace": namespace,
        "type": type_,
        "repository": repository,
        "tag": tag,
        "version": match.group("version"),
        "actor": str(fields["actor"] or "release-workflow"),
        "run_url": str(fields["run_url"] or ""),
    }


def _ensure_provider(request: Mapping[str, str], *, settings: Settings) -> dict[str, Any]:
    """The provider row at the upload's address, created without a GitHub binding when none exists yet."""
    namespace, type_ = request["namespace"], request["type"]
    table = repositories.registry(settings)
    row = {
        **providers.provider_row_key(namespace, type_),
        "namespace": namespace,
        "type": type_,
        "vcs_repo": request["repository"],
        "created_by": f"upload:{request['actor']}",
        "created_at": now_iso(),
    }
    try:
        table.put(row, condition=Attr("pk").not_exists())
    except ConditionFailed:
        found = table.get(providers.provider_row_key(namespace, type_))
        return dict(found) if found else row
    _log.info(
        "Created a provider from a release workflow upload.",
        extra={"event": "registry.provider_upload.created", "provider_address": f"{namespace}/{type_}"},
    )
    return row


def handle_upload(record: Mapping[str, Any], *, settings: Settings | None = None) -> str:
    """Publish the version a release workflow uploaded to the artifacts bucket, returning the outcome.

    The workflow's OIDC role is the only principal besides this function that may
    write under the uploads prefix, and the signature is still checked against this
    environment's key, so an upload publishes exactly what a GitHub release would.
    The folder is deleted once the version is settled or refused; a fault reaching S3,
    SSM or DynamoDB raises first, so SQS retries with the files still there.

    Raises:
        MalformedDelivery: The body is not a `provider_upload` message.
    """
    resolved = settings or get_settings()
    message = parse_upload_message(record)
    bucket, key = str(message["bucket"]), str(message["key"])
    extra = {"bucket": bucket, "key": key}
    match = _UPLOAD_KEY.match(key)
    if bucket != resolved.ARTIFACTS_BUCKET or match is None:
        _log.warning(
            "Ignored an upload outside the uploads prefix.", extra={"event": "registry.upload.ignored", **extra}
        )
        return SKIPPED
    s3 = _s3(resolved)
    folder = key[: -len("upload.json")]
    try:
        request = _upload_request(s3, bucket, key, match)
    except InvalidRelease as error:
        _log.warning(
            "Rejected a provider upload.", extra={"event": "registry.upload.rejected", "reason": str(error), **extra}
        )
        _discard_upload(s3, bucket, folder)
        return service.FAILED
    provider = _ensure_provider(request, settings=resolved)
    claim = {**request, "repo": request["repository"], "delivery": f"upload-{match.group('upload')}"}

    def source() -> tuple[Collection[str], Fetch]:
        """The uploaded file names and a fetch from the folder."""
        return _upload_names(s3, bucket, folder), _s3_fetch(s3, bucket, folder)

    outcome = _publish(provider, claim, source, settings=resolved)
    _discard_upload(s3, bucket, folder)
    _log.info(
        "Settled a provider upload.",
        extra={"event": "registry.upload.settled", "outcome": outcome, "run_url": request["run_url"], **extra},
    )
    return outcome


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
            installation_id=str(provider["vcs_installation_id"]),
            repository=str(provider["vcs_repo"]),
            max_pages=MAX_RELEASE_PAGES,
        )
    found: dict[str, str] = {}
    for release in listed:
        if release.draft:
            continue
        tag = release.tag_name
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
    "UPLOAD",
    "handle_release",
    "handle_sync",
    "handle_upload",
    "parse_sums",
    "parse_sync_message",
    "parse_upload_message",
    "publish",
    "signing_key",
]
