"""The module registry: modules connected to repositories, and the reads the protocol serves.

A module is created connected to a GitHub repository the environment's App is
installed on, the way HCP Terraform publishes a module from VCS. From then on a
push of a semantic version tag to that repository publishes that version: the
webhook route queues the tag, and the tag consumer reads the tarball through the
App's installation token, checks it and stores it. Connecting also queues a tag
sync, which imports the repository's existing semantic version tags through the
same queue and the same consumer, and `resync_module` queues one again, which is
how a tag GitHub never sent a push event for is picked up. The namespace is the
repository owner; the name and provider come from the request, or from a
`terraform-<provider>-<name>` repository name.

The table holds two kinds of row under one partition per module,
`MODULE#<namespace>/<name>/<provider>` lowercased, so a lookup is case insensitive
while the rows keep the case the module was created with. The module row at sort
key `MODULE` carries the connected repository. Each version row at
`VERSION#<version>` moves from `pending` to `published` or `failed`, and a
published version is immutable.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Iterable, Mapping, Optional, cast

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.github import repositories as github_repositories
from . import docs

if TYPE_CHECKING:  # pragma: no cover
    import httpx

_log = logging.getLogger(__name__)

MODULES_PREFIX: Final = "registry/modules/"
MODULE_CONTENT_TYPE: Final = "application/gzip"
DOWNLOAD_URL_TTL: Final = 300

MODULE_PK_PREFIX: Final = "MODULE#"
MODULE_SK: Final = "MODULE"
VERSION_SK_PREFIX: Final = "VERSION#"

PENDING: Final = "pending"
PUBLISHED: Final = "published"
FAILED: Final = "failed"

SYNC_KIND: Final = "module_sync"
"""The `kind` of a queued request to import a module's existing tags."""

_REPOSITORY_NAME = re.compile(r"^terraform-(?P<provider>[0-9a-z]+)-(?P<name>.+)$")
_NAME = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?$")
_PROVIDER = re.compile(r"^[0-9a-z]{1,64}$")
_VERSION = re.compile(r"^(?P<core>[0-9]+\.[0-9]+\.[0-9]+)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


class InvalidModuleName(Exception):
    """The request and the repository's name together name no valid module."""


class ModuleExists(Exception):
    """A module already sits at the address."""


class ModuleNotFound(Exception):
    """Nothing answers the address."""


class RegistryUnavailable(Exception):
    """The environment has no GitHub App, so no repository can be connected."""


class SyncUnavailable(Exception):
    """The tag sync could not be queued, so nothing will import the tags."""


def module_pk(namespace: str, name: str, provider: str) -> str:
    """The partition every row of one module shares, lowercased."""
    return MODULE_PK_PREFIX + f"{namespace}/{name}/{provider}".lower()


def module_row_key(namespace: str, name: str, provider: str) -> dict[str, str]:
    """The key of one module row."""
    return {"pk": module_pk(namespace, name, provider), "sk": MODULE_SK}


def version_sk(version: str) -> str:
    """The sort key of one version row."""
    return f"{VERSION_SK_PREFIX}{version}"


def module_prefix(namespace: str, name: str, provider: str) -> str:
    """The artifacts bucket prefix every version tarball of one module sits under."""
    return f"{MODULES_PREFIX}{namespace}/{name}/{provider}/".lower()


def module_key(namespace: str, name: str, provider: str, version: str) -> str:
    """Where a published version's tarball lives. Never expired by the bucket.

    It ends in `.tar.gz` so Terraform's getter reads the presigned URL as an archive.
    """
    return module_prefix(namespace, name, provider) + f"{version}.tar.gz"


def docs_key(namespace: str, name: str, provider: str, version: str) -> str:
    """Where a published version's extracted documentation lives, beside its tarball.

    Under the module's prefix, so deleting the module takes it too.
    """
    return module_prefix(namespace, name, provider) + f"{version}.docs.json"


def module_for(repository: str, name: Optional[str], provider: Optional[str]) -> tuple[str, str, str]:
    """The namespace, name and provider a module connected to `repository` gets.

    The namespace is the repository owner. A name or provider the request leaves
    out comes from a `terraform-<provider>-<name>` repository name.

    Raises:
        InvalidModuleName: The result is not a valid module address.
    """
    owner, _, repo_name = repository.partition("/")
    match = _REPOSITORY_NAME.match(repo_name)
    derived_name = match.group("name") if match else ""
    derived_provider = match.group("provider") if match else ""
    module_name = name or derived_name
    module_provider = provider or derived_provider
    if not module_name or not module_provider:
        raise InvalidModuleName(
            f"{repository} is not named terraform-<provider>-<name>, so the module needs a name and a provider"
        )
    if not _NAME.match(owner) or not _NAME.match(module_name) or not _PROVIDER.match(module_provider):
        raise InvalidModuleName(f"{owner}/{module_name}/{module_provider} is not a valid module address")
    return owner, module_name, module_provider


def version_order(version: str) -> tuple[Any, ...]:
    """A sort key putting versions in semantic order, a prerelease before its release."""
    match = _VERSION.match(version)
    if match is None:
        return ((), 0, ())
    core = tuple(int(part) for part in match.group("core").split("."))
    pre = match.group("pre")
    if pre is None:
        return (core, 1, ())
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))
    return (core, 0, parts)


def http_client() -> httpx.Client | None:
    """The HTTP client repository resolution goes through; `None` lets the App client build its own.

    The seam the tests replace with a mock transport.
    """
    return None


def sqs_client(settings: Settings) -> Any:
    """An SQS client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("sqs", region_name=settings.AWS_REGION_NAME or None)


def request_sync(row: Mapping[str, Any], actor: Optional[str], *, settings: Settings) -> str:
    """Queue an import of the connected repository's tags for one module, returning its delivery id.

    Raises:
        SyncUnavailable: There is no ingest queue here, or SQS refused the message.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    address = f"{row['namespace']}/{row['name']}/{row['provider']}"
    if not settings.REGISTRY_INGEST_QUEUE_URL:
        raise SyncUnavailable(f"no registry ingest queue to sync {address} through")
    delivery = f"sync-{uuid.uuid4().hex}"
    body = {
        "kind": SYNC_KIND,
        "delivery": delivery,
        "module": str(row["pk"]),
        "actor": actor or "",
        "requested_at_ms": int(time.time() * 1000),
    }
    try:
        sqs_client(settings).send_message(
            QueueUrl=settings.REGISTRY_INGEST_QUEUE_URL, MessageBody=json.dumps(body, separators=(",", ":"))
        )
    except (BotoCoreError, ClientError) as error:
        raise SyncUnavailable(f"the tag sync for {address} could not be queued") from error
    _log.info(
        "Queued a tag sync.",
        extra={"event": "registry.sync_queued", "module_address": address, "delivery": delivery},
    )
    return delivery


def create_module(
    vcs_repo: str,
    name: Optional[str],
    provider: Optional[str],
    actor: Optional[str],
    *,
    import_tags: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Connect a new module to a repository the App is installed on, then queue its tag import.

    With `import_tags` false the repository's existing tags wait for a resync. A
    sync that cannot be queued is logged rather than failing the connection, since
    a resync recovers it.

    Raises:
        InvalidModuleName: The address is not valid.
        RegistryUnavailable: The environment has no GitHub App.
        RepositoryNotInstalled: The App is not installed on the repository.
        ModuleExists: A module already sits at the address.
    """
    resolved = settings or get_settings()
    found = github_repositories.resolve_repository(vcs_repo, settings=resolved, client=http_client())
    if found is None:
        raise RegistryUnavailable("no GitHub App is configured")
    namespace, name, provider = module_for(found.full_name, name, provider)
    row = {
        **module_row_key(namespace, name, provider),
        "namespace": namespace,
        "name": name,
        "provider": provider,
        "vcs_repo": found.full_name,
        "vcs_repository_id": found.repository_id,
        "vcs_installation_id": found.installation_id,
        "created_by": actor or "",
        "created_at": now_iso(),
    }
    try:
        repositories.registry(resolved).put(row, condition=Attr("pk").not_exists())
    except ConditionFailed as error:
        raise ModuleExists(f"{namespace}/{name}/{provider}") from error
    _log.info(
        "Connected a module to a repository.",
        extra={
            "event": "registry.module_created",
            "module_address": f"{namespace}/{name}/{provider}",
            "repository": found.full_name,
        },
    )
    if import_tags:
        try:
            request_sync(row, actor, settings=resolved)
        except SyncUnavailable as error:
            _log.warning(
                "Connected a module without queueing its tag import.",
                extra={"event": "registry.sync_unqueued", "module_address": f"{namespace}/{name}/{provider}"},
                exc_info=error,
            )
    return module_view(row, [])


def connected_module(namespace: str, name: str, provider: str, *, settings: Settings) -> Optional[dict[str, Any]]:
    """The module row at the address when it is connected to a repository, else `None`."""
    row = repositories.registry(settings).get(module_row_key(namespace, name, provider))
    if not row or not row.get("vcs_repo") or not row.get("vcs_installation_id"):
        return None
    return dict(row)


def resync_module(
    namespace: str, name: str, provider: str, actor: Optional[str], *, settings: Settings | None = None
) -> dict[str, str]:
    """Queue an import of every semantic version tag the module's repository holds.

    Like HCP Terraform's resync: it picks up tags pushed while nothing was
    connected, or pushed more than three at a time, when GitHub sends no event.

    Raises:
        ModuleNotFound: No connected module sits at the address.
        SyncUnavailable: The sync could not be queued.
    """
    resolved = settings or get_settings()
    row = connected_module(namespace, name, provider, settings=resolved)
    if row is None:
        raise ModuleNotFound(f"{namespace}/{name}/{provider}")
    delivery = request_sync(row, actor, settings=resolved)
    return {"source": f"{row['namespace']}/{row['name']}/{row['provider']}", "delivery": delivery}


def connected_modules(repository_id: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every module row connected to the repository with this id.

    A scan, since the registry holds a handful of modules and a tag push is rare.
    """
    resolved = settings or get_settings()
    return list(
        repositories.registry(resolved).iter_scan(
            filter_expression=Attr("sk").eq(MODULE_SK) & Attr("vcs_repository_id").eq(str(repository_id))
        )
    )


def version_rows(namespace: str, name: str, provider: str, *, settings: Settings) -> Iterable[dict[str, Any]]:
    """Every version row of one module, whatever its status."""
    return repositories.registry(settings).iter_query(
        Key("pk").eq(module_pk(namespace, name, provider)) & Key("sk").begins_with(VERSION_SK_PREFIX)
    )


def published_versions(namespace: str, name: str, provider: str, *, settings: Settings | None = None) -> list[str]:
    """The module's published versions, newest first.

    Raises:
        ModuleNotFound: The module has no published version.
    """
    resolved = settings or get_settings()
    versions = [
        str(row["version"])
        for row in version_rows(namespace, name, provider, settings=resolved)
        if row.get("status") == PUBLISHED
    ]
    if not versions:
        raise ModuleNotFound(f"{namespace}/{name}/{provider}")
    return sorted(versions, key=version_order, reverse=True)


def download_url(namespace: str, name: str, provider: str, version: str, *, settings: Settings | None = None) -> str:
    """A short lived presigned GET for one published version's tarball.

    Raises:
        ModuleNotFound: The version does not exist or is not published.
    """
    from webbpulse.storage import presigned_get

    resolved = settings or get_settings()
    row = repositories.registry(resolved).get({"pk": module_pk(namespace, name, provider), "sk": version_sk(version)})
    if not row or row.get("status") != PUBLISHED or not row.get("key"):
        raise ModuleNotFound(f"{namespace}/{name}/{provider} {version}")
    return presigned_get(
        resolved.ARTIFACTS_BUCKET,
        str(row["key"]),
        DOWNLOAD_URL_TTL,
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.s3_endpoint_url,
    ).url


def _version_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One version row as the listing renders it."""
    return {
        "version": str(row["version"]),
        "status": str(row.get("status", PENDING)),
        "error": row.get("error"),
        "repository": str(row.get("repository", "")),
        "tag": row.get("tag"),
        "sha": str(row.get("sha", "")),
        "actor": str(row.get("actor", "")),
        "created_at": str(row.get("created_at", "")),
        "published_at": row.get("published_at"),
        "size_bytes": int(row["size_bytes"]) if row.get("size_bytes") is not None else None,
    }


def module_view(row: Mapping[str, Any], versions: list[Mapping[str, Any]]) -> dict[str, Any]:
    """One module as the API renders it, from its module row or its first version row."""
    namespace, name, provider = str(row["namespace"]), str(row["name"]), str(row["provider"])
    views = sorted((_version_view(version) for version in versions), key=lambda view: version_order(view["version"]))
    return {
        "namespace": namespace,
        "name": name,
        "provider": provider,
        "source": f"{namespace}/{name}/{provider}",
        "vcs_repo": row.get("vcs_repo") if row.get("sk") == MODULE_SK else None,
        "created_at": row.get("created_at") if row.get("sk") == MODULE_SK else None,
        "versions": list(reversed(views)),
    }


def list_modules(*, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every module with every version, failed and pending ones included.

    A scan, since the registry holds a handful of modules and the listing is an
    operator view. A module with versions but no module row is listed without a
    repository. Modules sort by address and versions newest first.
    """
    resolved = settings or get_settings()
    heads: dict[str, Mapping[str, Any]] = {}
    versions: dict[str, list[Mapping[str, Any]]] = {}
    for row in repositories.registry(resolved).iter_scan(filter_expression=Attr("pk").begins_with(MODULE_PK_PREFIX)):
        pk = str(row["pk"])
        if row.get("sk") == MODULE_SK:
            heads[pk] = row
        else:
            heads.setdefault(pk, row)
            versions.setdefault(pk, []).append(row)
    return [module_view(heads[pk], versions.get(pk, [])) for pk in sorted(heads)]


def get_module(namespace: str, name: str, provider: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One module and every version of it.

    Raises:
        ModuleNotFound: Nothing sits at the address.
    """
    resolved = settings or get_settings()
    rows = list(repositories.registry(resolved).iter_query(Key("pk").eq(module_pk(namespace, name, provider))))
    if not rows:
        raise ModuleNotFound(f"{namespace}/{name}/{provider}")
    head = next((row for row in rows if row.get("sk") == MODULE_SK), rows[0])
    return module_view(head, [row for row in rows if row.get("sk") != MODULE_SK])


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def store_docs(
    tarball: Path, namespace: str, name: str, provider: str, version: str, *, settings: Settings
) -> dict[str, Any]:
    """Extract the documentation of a packed module and store it beside the tarball."""
    extracted = docs.extract(tarball)
    _s3(settings).put_object(
        Bucket=settings.ARTIFACTS_BUCKET,
        Key=docs_key(namespace, name, provider, version),
        Body=json.dumps(extracted, separators=(",", ":")).encode(),
        ContentType="application/json",
    )
    return extracted


def _stored_docs(key: str, *, settings: Settings) -> Optional[dict[str, Any]]:
    """The stored documentation at `key` when present and of the current shape."""
    from botocore.exceptions import ClientError

    try:
        body = _s3(settings).get_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key)["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    try:
        stored = json.loads(body)
    except ValueError:
        return None
    if not isinstance(stored, dict) or stored.get("schema") != docs.DOCS_SCHEMA:
        return None
    return cast(dict[str, Any], stored)


def _docs_for(row: Mapping[str, Any], *, settings: Settings) -> Optional[dict[str, Any]]:
    """A published version's documentation, extracted now when it never was or its shape is old.

    `None` when the extraction fails, which is logged; the page still shows the version.
    """
    namespace, name, provider, version = (str(row[field]) for field in ("namespace", "name", "provider", "version"))
    stored = _stored_docs(docs_key(namespace, name, provider, version), settings=settings)
    if stored is not None:
        return stored
    try:
        with tempfile.TemporaryDirectory() as scratch:
            tarball = Path(scratch) / "module.tar.gz"
            _s3(settings).download_file(settings.ARTIFACTS_BUCKET, str(row["key"]), str(tarball))
            return store_docs(tarball, namespace, name, provider, version, settings=settings)
    except Exception as error:  # noqa: BLE001
        _log.warning(
            "Could not extract a module version's documentation.",
            extra={
                "event": "registry.docs.unavailable",
                "module_address": f"{namespace}/{name}/{provider}",
                "version": version,
            },
            exc_info=error,
        )
        return None


def get_version(
    namespace: str, name: str, provider: str, version: str, *, settings: Settings | None = None
) -> dict[str, Any]:
    """One version of a module with its documentation, as the module page shows it.

    Only a published version has documentation; a pending or failed one carries `None`.

    Raises:
        ModuleNotFound: The module has no such version.
    """
    resolved = settings or get_settings()
    table = repositories.registry(resolved)
    row = table.get({"pk": module_pk(namespace, name, provider), "sk": version_sk(version)})
    if not row:
        raise ModuleNotFound(f"{namespace}/{name}/{provider} {version}")
    head = table.get(module_row_key(namespace, name, provider)) or row
    view = module_view(head, [])
    del view["versions"]
    published = row.get("status") == PUBLISHED and bool(row.get("key"))
    return {**view, "version": _version_view(row), "docs": _docs_for(row, settings=resolved) if published else None}


def delete_module(namespace: str, name: str, provider: str, *, settings: Settings | None = None) -> None:
    """Remove a module, every version of it and every stored tarball.

    Raises:
        ModuleNotFound: Nothing sits at the address.
    """
    import boto3

    resolved = settings or get_settings()
    table = repositories.registry(resolved)
    rows = list(table.iter_query(Key("pk").eq(module_pk(namespace, name, provider))))
    if not rows:
        raise ModuleNotFound(f"{namespace}/{name}/{provider}")
    s3 = boto3.client("s3", region_name=resolved.AWS_REGION_NAME or None, endpoint_url=resolved.s3_endpoint_url)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=resolved.ARTIFACTS_BUCKET, Prefix=module_prefix(namespace, name, provider)):
        keys: list[Any] = [{"Key": item.get("Key", "")} for item in page.get("Contents", []) if item.get("Key")]
        if keys:
            s3.delete_objects(Bucket=resolved.ARTIFACTS_BUCKET, Delete={"Objects": keys, "Quiet": True})
    table.delete_many([{"pk": str(row["pk"]), "sk": str(row["sk"])} for row in rows])
    _log.info(
        "Deleted a module.",
        extra={"event": "registry.module_deleted", "module_address": f"{namespace}/{name}/{provider}"},
    )


__all__ = [
    "DOWNLOAD_URL_TTL",
    "FAILED",
    "MODULES_PREFIX",
    "MODULE_CONTENT_TYPE",
    "MODULE_SK",
    "PENDING",
    "PUBLISHED",
    "InvalidModuleName",
    "ModuleExists",
    "ModuleNotFound",
    "RegistryUnavailable",
    "SYNC_KIND",
    "SyncUnavailable",
    "connected_module",
    "connected_modules",
    "create_module",
    "delete_module",
    "docs_key",
    "download_url",
    "get_module",
    "get_version",
    "http_client",
    "list_modules",
    "module_for",
    "module_key",
    "module_pk",
    "module_prefix",
    "module_row_key",
    "module_view",
    "published_versions",
    "request_sync",
    "resync_module",
    "sqs_client",
    "store_docs",
    "version_order",
    "version_rows",
    "version_sk",
]
