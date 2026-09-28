"""The provider registry: providers connected to repositories, and the reads the protocol serves.

A provider is connected to a GitHub repository named `terraform-provider-<type>`
that the environment's App is installed on, and each published release there with a
semantic version tag publishes that version. The release must carry GoReleaser's
registry layout: one `<repo>_<version>_<os>_<arch>.zip` per platform, a
`<repo>_<version>_SHA256SUMS` listing them, its detached signature
`<repo>_<version>_SHA256SUMS.sig`, and optionally `<repo>_<version>_manifest.json`
naming the plugin protocol versions.

The signature must verify against the public key in this environment's SSM
parameter, so staging publishes what the staging key signed, prereleases included,
and production only what the production key signed. The key is stored on the
version row, so a later key rotation leaves published versions installable.

Rows share the registry table with modules under `PROVIDER#<namespace>/<type>`
lowercased: the provider row at sort key `PROVIDER` and one row per version at
`VERSION#<version>`, moving from `pending` to `published` or `failed`. Artifacts
live under `registry/providers/` in the artifacts bucket and are served through
short lived presigned URLs.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Final, Iterable, Mapping, Optional

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.github import repositories as github_repositories
from . import service

_log = logging.getLogger(__name__)

PROVIDERS_PREFIX: Final = "registry/providers/"
PROVIDER_PK_PREFIX: Final = "PROVIDER#"
PROVIDER_SK: Final = "PROVIDER"
DOWNLOAD_URL_TTL: Final = 900
DEFAULT_PROTOCOLS: Final = ("5.0",)
"""What Terraform assumes of a release with no manifest."""

SYNC_KIND: Final = "provider_sync"
"""The `kind` of a queued request to import a provider's existing releases."""

_REPOSITORY_NAME = re.compile(r"^terraform-provider-(?P<type>[0-9a-z][0-9a-z-]{0,62})$")
_NAMESPACE = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?$")


class InvalidProviderName(Exception):
    """The repository's name does not name a provider."""


class ProviderExists(Exception):
    """A provider already sits at the address."""


class ProviderNotFound(Exception):
    """Nothing answers the address."""


def provider_pk(namespace: str, type_: str) -> str:
    """The partition every row of one provider shares, lowercased."""
    return PROVIDER_PK_PREFIX + f"{namespace}/{type_}".lower()


def provider_row_key(namespace: str, type_: str) -> dict[str, str]:
    """The key of one provider row."""
    return {"pk": provider_pk(namespace, type_), "sk": PROVIDER_SK}


def provider_prefix(namespace: str, type_: str) -> str:
    """The artifacts bucket prefix every file of one provider sits under."""
    return f"{PROVIDERS_PREFIX}{namespace}/{type_}/".lower()


def artifact_key(namespace: str, type_: str, version: str, filename: str) -> str:
    """Where one released file of a version lives."""
    return provider_prefix(namespace, type_) + f"{version}/{filename}"


def provider_for(repository: str) -> tuple[str, str]:
    """The namespace and type a provider connected to `repository` gets.

    Raises:
        InvalidProviderName: The repository is not named `terraform-provider-<type>`.
    """
    owner, _, repo_name = repository.partition("/")
    match = _REPOSITORY_NAME.match(repo_name.lower())
    if match is None or not _NAMESPACE.match(owner):
        raise InvalidProviderName(f"{repository} is not named terraform-provider-<type>")
    return owner, match.group("type")


def request_sync(row: Mapping[str, Any], actor: Optional[str], *, settings: Settings) -> str:
    """Queue an import of the connected repository's releases for one provider, returning its delivery id.

    Raises:
        SyncUnavailable: There is no ingest queue here, or SQS refused the message.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    address = f"{row['namespace']}/{row['type']}"
    if not settings.REGISTRY_INGEST_QUEUE_URL:
        raise service.SyncUnavailable(f"no registry ingest queue to sync {address} through")
    delivery = f"sync-{uuid.uuid4().hex}"
    body = {
        "kind": SYNC_KIND,
        "delivery": delivery,
        "provider": str(row["pk"]),
        "actor": actor or "",
        "requested_at_ms": int(time.time() * 1000),
    }
    try:
        service.sqs_client(settings).send_message(
            QueueUrl=settings.REGISTRY_INGEST_QUEUE_URL, MessageBody=json.dumps(body, separators=(",", ":"))
        )
    except (BotoCoreError, ClientError) as error:
        raise service.SyncUnavailable(f"the release sync for {address} could not be queued") from error
    _log.info(
        "Queued a release sync.",
        extra={"event": "registry.provider_sync_queued", "provider_address": address, "delivery": delivery},
    )
    return delivery


def create_provider(
    vcs_repo: str, actor: Optional[str], *, import_releases: bool = True, settings: Settings | None = None
) -> dict[str, Any]:
    """Connect a new provider to a repository the App is installed on, then queue its release import.

    Raises:
        InvalidProviderName: The repository is not named `terraform-provider-<type>`.
        RegistryUnavailable: The environment has no GitHub App.
        RepositoryNotInstalled: The App is not installed on the repository.
        ProviderExists: A provider already sits at the address.
    """
    resolved = settings or get_settings()
    provider_for(vcs_repo)
    found = github_repositories.resolve_repository(vcs_repo, settings=resolved, client=service.http_client())
    if found is None:
        raise service.RegistryUnavailable("no GitHub App is configured")
    namespace, type_ = provider_for(found.full_name)
    row = {
        **provider_row_key(namespace, type_),
        "namespace": namespace,
        "type": type_,
        "vcs_repo": found.full_name,
        "vcs_repository_id": found.repository_id,
        "vcs_installation_id": found.installation_id,
        "created_by": actor or "",
        "created_at": now_iso(),
    }
    try:
        repositories.registry(resolved).put(row, condition=Attr("pk").not_exists())
    except ConditionFailed as error:
        raise ProviderExists(f"{namespace}/{type_}") from error
    _log.info(
        "Connected a provider to a repository.",
        extra={
            "event": "registry.provider_created",
            "provider_address": f"{namespace}/{type_}",
            "repository": found.full_name,
        },
    )
    if import_releases:
        try:
            request_sync(row, actor, settings=resolved)
        except service.SyncUnavailable as error:
            _log.warning(
                "Connected a provider without queueing its release import.",
                extra={"event": "registry.provider_sync_unqueued", "provider_address": f"{namespace}/{type_}"},
                exc_info=error,
            )
    return provider_view(row, [])


def connected_provider(namespace: str, type_: str, *, settings: Settings) -> Optional[dict[str, Any]]:
    """The provider row at the address, or `None`."""
    row = repositories.registry(settings).get(provider_row_key(namespace, type_))
    return dict(row) if row else None


def connected_providers(repository_id: str, *, settings: Settings) -> list[dict[str, Any]]:
    """Every provider row connected to the repository with this id. A scan, as for modules."""
    return list(
        repositories.registry(settings).iter_scan(
            filter_expression=Attr("sk").eq(PROVIDER_SK) & Attr("vcs_repository_id").eq(str(repository_id))
        )
    )


def resync_provider(
    namespace: str, type_: str, actor: Optional[str], *, settings: Settings | None = None
) -> dict[str, str]:
    """Queue an import of every release the provider's repository holds.

    Raises:
        ProviderNotFound: No provider sits at the address.
        SyncUnavailable: The sync could not be queued.
    """
    resolved = settings or get_settings()
    row = connected_provider(namespace, type_, settings=resolved)
    if row is None:
        raise ProviderNotFound(f"{namespace}/{type_}")
    delivery = request_sync(row, actor, settings=resolved)
    return {"source": f"{row['namespace']}/{row['type']}", "delivery": delivery}


def version_rows(namespace: str, type_: str, *, settings: Settings) -> Iterable[dict[str, Any]]:
    """Every version row of one provider, whatever its status."""
    return repositories.registry(settings).iter_query(
        Key("pk").eq(provider_pk(namespace, type_)) & Key("sk").begins_with(service.VERSION_SK_PREFIX)
    )


def published_versions(namespace: str, type_: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """The provider's published versions in the protocol's shape, newest first.

    Raises:
        ProviderNotFound: The provider has no published version.
    """
    resolved = settings or get_settings()
    rows = [row for row in version_rows(namespace, type_, settings=resolved) if row.get("status") == service.PUBLISHED]
    if not rows:
        raise ProviderNotFound(f"{namespace}/{type_}")
    rows.sort(key=lambda row: service.version_order(str(row["version"])), reverse=True)
    return [
        {
            "version": str(row["version"]),
            "protocols": [str(protocol) for protocol in row.get("protocols") or DEFAULT_PROTOCOLS],
            "platforms": [{"os": str(item["os"]), "arch": str(item["arch"])} for item in row.get("platforms") or []],
        }
        for row in rows
    ]


def _presign(key: str, settings: Settings) -> str:
    """A short lived presigned GET for one stored file."""
    from webbpulse.storage import presigned_get

    return presigned_get(
        settings.ARTIFACTS_BUCKET,
        key,
        DOWNLOAD_URL_TTL,
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
    ).url


def download(
    namespace: str, type_: str, version: str, os_: str, arch: str, *, settings: Settings | None = None
) -> dict[str, Any]:
    """The protocol's download answer for one platform of a published version.

    Raises:
        ProviderNotFound: The version is not published or has no build for the platform.
    """
    resolved = settings or get_settings()
    row = repositories.registry(resolved).get({"pk": provider_pk(namespace, type_), "sk": service.version_sk(version)})
    if not row or row.get("status") != service.PUBLISHED:
        raise ProviderNotFound(f"{namespace}/{type_} {version}")
    platform = next(
        (item for item in row.get("platforms") or [] if item.get("os") == os_ and item.get("arch") == arch), None
    )
    if platform is None:
        raise ProviderNotFound(f"{namespace}/{type_} {version} for {os_}_{arch}")
    return {
        "protocols": [str(protocol) for protocol in row.get("protocols") or DEFAULT_PROTOCOLS],
        "os": os_,
        "arch": arch,
        "filename": str(platform["filename"]),
        "download_url": _presign(str(platform["key"]), resolved),
        "shasums_url": _presign(str(row["shasums_key"]), resolved),
        "shasums_signature_url": _presign(str(row["signature_key"]), resolved),
        "shasum": str(platform["shasum"]),
        "signing_keys": {
            "gpg_public_keys": [
                {
                    "key_id": str(row["key_id"]),
                    "ascii_armor": str(row["ascii_armor"]),
                    "trust_signature": "",
                    "source": "",
                    "source_url": None,
                }
            ]
        },
    }


def _version_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One version row as the listing renders it."""
    return {
        "version": str(row["version"]),
        "status": str(row.get("status", service.PENDING)),
        "error": row.get("error"),
        "tag": str(row.get("tag", "")),
        "protocols": [str(protocol) for protocol in row.get("protocols") or []],
        "platforms": [
            {
                "os": str(item["os"]),
                "arch": str(item["arch"]),
                "filename": str(item["filename"]),
                "shasum": str(item["shasum"]),
            }
            for item in row.get("platforms") or []
        ],
        "key_id": row.get("key_id"),
        "actor": str(row.get("actor", "")),
        "created_at": str(row.get("created_at", "")),
        "published_at": row.get("published_at"),
    }


def provider_view(row: Mapping[str, Any], versions: list[Mapping[str, Any]]) -> dict[str, Any]:
    """One provider as the API renders it."""
    views = sorted(
        (_version_view(version) for version in versions),
        key=lambda view: service.version_order(view["version"]),
        reverse=True,
    )
    return {
        "namespace": str(row["namespace"]),
        "type": str(row["type"]),
        "source": f"{row['namespace']}/{row['type']}",
        "vcs_repo": str(row.get("vcs_repo", "")),
        "created_at": row.get("created_at"),
        "versions": views,
    }


def list_providers(*, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every connected provider with every version, sorted by address."""
    resolved = settings or get_settings()
    heads: dict[str, Mapping[str, Any]] = {}
    versions: dict[str, list[Mapping[str, Any]]] = {}
    for row in repositories.registry(resolved).iter_scan(filter_expression=Attr("pk").begins_with(PROVIDER_PK_PREFIX)):
        pk = str(row["pk"])
        if row.get("sk") == PROVIDER_SK:
            heads[pk] = row
        else:
            versions.setdefault(pk, []).append(row)
    return [provider_view(heads[pk], versions.get(pk, [])) for pk in sorted(heads)]


def get_provider(namespace: str, type_: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One provider and every version of it.

    Raises:
        ProviderNotFound: No provider sits at the address.
    """
    resolved = settings or get_settings()
    rows = list(repositories.registry(resolved).iter_query(Key("pk").eq(provider_pk(namespace, type_))))
    head = next((row for row in rows if row.get("sk") == PROVIDER_SK), None)
    if head is None:
        raise ProviderNotFound(f"{namespace}/{type_}")
    return provider_view(head, [row for row in rows if row.get("sk") != PROVIDER_SK])


def delete_provider(namespace: str, type_: str, *, settings: Settings | None = None) -> None:
    """Remove a provider, every version of it and every stored file.

    Raises:
        ProviderNotFound: No provider sits at the address.
    """
    import boto3

    resolved = settings or get_settings()
    table = repositories.registry(resolved)
    rows = list(table.iter_query(Key("pk").eq(provider_pk(namespace, type_))))
    if not rows:
        raise ProviderNotFound(f"{namespace}/{type_}")
    s3 = boto3.client("s3", region_name=resolved.AWS_REGION_NAME or None, endpoint_url=resolved.s3_endpoint_url)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=resolved.ARTIFACTS_BUCKET, Prefix=provider_prefix(namespace, type_)):
        keys: list[Any] = [{"Key": item.get("Key", "")} for item in page.get("Contents", []) if item.get("Key")]
        if keys:
            s3.delete_objects(Bucket=resolved.ARTIFACTS_BUCKET, Delete={"Objects": keys, "Quiet": True})
    table.delete_many([{"pk": str(row["pk"]), "sk": str(row["sk"])} for row in rows])
    _log.info(
        "Deleted a provider.",
        extra={"event": "registry.provider_deleted", "provider_address": f"{namespace}/{type_}"},
    )


__all__ = [
    "DEFAULT_PROTOCOLS",
    "DOWNLOAD_URL_TTL",
    "PROVIDERS_PREFIX",
    "PROVIDER_PK_PREFIX",
    "PROVIDER_SK",
    "SYNC_KIND",
    "InvalidProviderName",
    "ProviderExists",
    "ProviderNotFound",
    "artifact_key",
    "connected_provider",
    "connected_providers",
    "create_provider",
    "delete_provider",
    "download",
    "get_provider",
    "list_providers",
    "provider_for",
    "provider_pk",
    "provider_prefix",
    "provider_row_key",
    "provider_view",
    "published_versions",
    "request_sync",
    "resync_provider",
    "version_rows",
]
