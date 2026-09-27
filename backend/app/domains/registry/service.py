"""The module registry: uploads from GitHub Actions, and the reads the protocol serves.

A module is published by a tag push in an allowlisted repository. The workflow
authenticates with its GitHub Actions OIDC token, and everything the registry
trusts about the upload comes from that token's verified claims: the namespace is
the repository owner, the name and provider come from the repository, and the
version from the tag. The version row is written `pending` before any URL exists,
and the ingest consumer moves it to `published` or `failed` once the tarball has
landed and been checked.

The table holds two kinds of row. A version row sits at
`MODULE#<namespace>/<name>/<provider>`, `VERSION#<version>`, lowercased so a lookup
is case insensitive while the row keeps the case it was published with. An upload
row sits at `UPLOAD#<upload_id>` and expires by TTL once its tarball has long been
ingested or abandoned.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Iterable, Mapping, Optional

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.github.oidc import InvalidActionsToken, crockford, verify_actions_token

_log = logging.getLogger(__name__)

UPLOAD_ID_PREFIX: Final = "up-"
UPLOAD_ID_PATTERN: Final = r"up-[0-9A-HJKMNP-TV-Z]{26}"
INCOMING_PREFIX: Final = "registry/incoming/"
MODULES_PREFIX: Final = "registry/modules/"
UPLOAD_CONTENT_TYPE: Final = "application/gzip"
UPLOAD_URL_TTL: Final = 900
DOWNLOAD_URL_TTL: Final = 300
UPLOAD_RECORD_TTL: Final = timedelta(days=7)
"""How long an upload row outlives its request, matching the bucket's lifecycle on
`registry/incoming/`."""

MODULE_PK_PREFIX: Final = "MODULE#"
VERSION_SK_PREFIX: Final = "VERSION#"
UPLOAD_PK_PREFIX: Final = "UPLOAD#"
UPLOAD_SK: Final = "UPLOAD"

PENDING: Final = "pending"
PUBLISHED: Final = "published"
FAILED: Final = "failed"

PUSH_EVENT: Final = "push"

TRUSTED_CLAIMS: Final = (
    "repository",
    "repository_id",
    "repository_owner",
    "event_name",
    "ref",
    "sha",
    "actor",
    "run_id",
    "run_attempt",
)
"""The claims an upload is attributed by. Every one is required."""

_TAG_REF = re.compile(
    r"^refs/tags/v?(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?)$"
)
_REPOSITORY_NAME = re.compile(r"^terraform-(?P<provider>[0-9a-z]+)-(?P<name>.+)$")
_NAME = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?$")
_PROVIDER = re.compile(r"^[0-9a-z]{1,64}$")
_VERSION = re.compile(r"^(?P<core>[0-9]+\.[0-9]+\.[0-9]+)(?:-(?P<pre>[0-9A-Za-z.-]+))?$")


class InvalidUploadToken(Exception):
    """The bearer is missing, fails verification, or lacks a trusted claim."""


class RepositoryNotAllowed(Exception):
    """The token's repository is not on the registry's allowlist."""


class UnsupportedRef(Exception):
    """The token is not for a push of a semantic version tag."""


class InvalidModuleName(Exception):
    """The repository's name or its override does not name a valid module."""


class VersionAlreadyPublished(Exception):
    """The version is already published, and published versions are immutable."""


class ModuleNotFound(Exception):
    """No published version answers the address."""


def module_pk(namespace: str, name: str, provider: str) -> str:
    """The partition every version of one module shares, lowercased."""
    return MODULE_PK_PREFIX + f"{namespace}/{name}/{provider}".lower()


def version_sk(version: str) -> str:
    """The sort key of one version row."""
    return f"{VERSION_SK_PREFIX}{version}"


def upload_key(upload_id: str) -> Mapping[str, str]:
    """The key of one upload row."""
    return {"pk": f"{UPLOAD_PK_PREFIX}{upload_id}", "sk": UPLOAD_SK}


def incoming_key(upload_id: str) -> str:
    """The artifacts bucket key an upload's tarball is PUT to."""
    return f"{INCOMING_PREFIX}{upload_id}.tar.gz"


def module_key(namespace: str, name: str, provider: str, version: str) -> str:
    """Where a published version's tarball lives. Never expired by the bucket.

    It ends in `.tar.gz` so Terraform's getter reads the presigned URL as an archive.
    """
    return f"{MODULES_PREFIX}{namespace}/{name}/{provider}/".lower() + f"{version}.tar.gz"


def allowlist(settings: Settings | None = None) -> dict[str, str]:
    """The repositories allowed to publish, lowercased `owner/name` to an override.

    An unreadable setting allows nothing, so a bad deploy fails closed.
    """
    resolved = settings or get_settings()
    try:
        raw = json.loads(resolved.REGISTRY_REPOSITORIES or "{}")
    except ValueError:
        _log.error("REGISTRY_REPOSITORIES is not JSON.", extra={"event": "registry.allowlist_invalid"})
        return {}
    if not isinstance(raw, Mapping):
        _log.error("REGISTRY_REPOSITORIES is not an object.", extra={"event": "registry.allowlist_invalid"})
        return {}
    return {str(repository).lower(): str(override or "") for repository, override in raw.items()}


def module_for(repository: str, owner: str, override: str) -> tuple[str, str, str]:
    """The namespace, name and provider a repository publishes as.

    Raises:
        InvalidModuleName: Neither the override nor the repository name gives a
            valid name and provider.
    """
    if override:
        name, _, provider = override.partition("/")
    else:
        match = _REPOSITORY_NAME.match(repository.split("/", 1)[-1])
        if match is None:
            raise InvalidModuleName(f"{repository} is not named terraform-<provider>-<name>")
        name, provider = match.group("name"), match.group("provider")
    if not _NAME.match(owner) or not _NAME.match(name) or not _PROVIDER.match(provider):
        raise InvalidModuleName(f"{owner}/{name}/{provider} is not a valid module address")
    return owner, name, provider


def version_from_ref(event: str, ref: str) -> str:
    """The version a tag push names, without its `v`.

    Raises:
        UnsupportedRef: Any event but a push, or a ref that is not a semantic
            version tag.
    """
    if event != PUSH_EVENT:
        raise UnsupportedRef(f"the {event} event does not publish modules")
    match = _TAG_REF.match(ref)
    if match is None:
        raise UnsupportedRef(f"{ref} is not a semantic version tag")
    return match.group("version")


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


def upload_id_for(claims: Mapping[str, Any], version: str) -> str:
    """The upload id one workflow run attempt's upload of one version gets.

    Deterministic, so a retried request reaches the same row and the same key.
    """
    seed = ":".join(
        [str(claims["repository_id"]), version, str(claims["sha"]), str(claims["run_id"]), str(claims["run_attempt"])]
    )
    return UPLOAD_ID_PREFIX + crockford(hashlib.sha256(seed.encode()).digest())


def verify_token(token: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """The verified claims of the publishing workflow's GitHub Actions OIDC token.

    Raises:
        InvalidUploadToken: No token, a token that fails verification, or one
            missing a trusted claim.
    """
    resolved = settings or get_settings()
    try:
        return verify_actions_token(token, audience=resolved.VCS_OIDC_AUDIENCE, required=TRUSTED_CLAIMS)
    except InvalidActionsToken as error:
        raise InvalidUploadToken(str(error)) from error


def issue_upload(token: str, size_bytes: int, *, settings: Settings | None = None) -> dict[str, Any]:
    """Verify the workflow's token, record a pending version and sign its PUT.

    Idempotent on the repository id, the version, the commit, the workflow run and
    its attempt: a repeat returns the same `upload_id` and a new URL for the same
    key. A failed or pending version may be uploaded again; a published one may not.

    Raises:
        InvalidUploadToken: The token did not verify.
        RepositoryNotAllowed: The repository is not on the allowlist.
        UnsupportedRef: The token is not for a version tag push.
        InvalidModuleName: The repository does not name a valid module.
        VersionAlreadyPublished: The version is already published.
    """
    from webbpulse.storage import presigned_put

    resolved = settings or get_settings()
    claims = verify_token(token, settings=resolved)
    repository = str(claims["repository"])
    allowed = allowlist(resolved)
    if repository.lower() not in allowed:
        raise RepositoryNotAllowed(repository)
    version = version_from_ref(str(claims["event_name"]), str(claims["ref"]))
    namespace, name, provider = module_for(repository, str(claims["repository_owner"]), allowed[repository.lower()])

    upload_id = upload_id_for(claims, version)
    now = datetime.now(timezone.utc)
    created_at = now_iso()
    table = repositories.registry(resolved)
    try:
        table.put(
            {
                "pk": module_pk(namespace, name, provider),
                "sk": version_sk(version),
                "namespace": namespace,
                "name": name,
                "provider": provider,
                "version": version,
                "status": PENDING,
                "upload_id": upload_id,
                "repository": repository,
                "repository_id": str(claims["repository_id"]),
                "sha": str(claims["sha"]),
                "actor": str(claims["actor"]),
                "created_at": created_at,
            },
            condition=Attr("pk").not_exists() | Attr("status").ne(PUBLISHED),
        )
    except ConditionFailed as error:
        raise VersionAlreadyPublished(f"{namespace}/{name}/{provider} {version}") from error
    table.put(
        {
            **upload_key(upload_id),
            "upload_id": upload_id,
            "namespace": namespace,
            "name": name,
            "provider": provider,
            "version": version,
            "repository": repository,
            "sha": str(claims["sha"]),
            "size_bytes": int(size_bytes),
            "created_at": created_at,
            "expires_at": int((now + UPLOAD_RECORD_TTL).timestamp()),
        }
    )

    upload = presigned_put(
        resolved.ARTIFACTS_BUCKET,
        incoming_key(upload_id),
        UPLOAD_CONTENT_TYPE,
        int(size_bytes),
        UPLOAD_URL_TTL,
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.s3_endpoint_url,
    )
    _log.info(
        "Issued a module upload URL.",
        extra={
            "event": "registry.upload_issued",
            "upload_id": upload_id,
            "repo": repository,
            "module_address": f"{namespace}/{name}/{provider}",
            "version": version,
        },
    )
    return {
        "upload_id": upload_id,
        "upload_url": upload.url,
        "headers": dict(upload.headers),
        "expires_in": UPLOAD_URL_TTL,
        "module": {"namespace": namespace, "name": name, "provider": provider, "version": version},
    }


def _versions(namespace: str, name: str, provider: str, *, settings: Settings) -> Iterable[dict[str, Any]]:
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
        for row in _versions(namespace, name, provider, settings=resolved)
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
        "sha": str(row.get("sha", "")),
        "actor": str(row.get("actor", "")),
        "created_at": str(row.get("created_at", "")),
        "published_at": row.get("published_at"),
        "size_bytes": int(row["size_bytes"]) if row.get("size_bytes") is not None else None,
    }


def list_modules(*, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every module with every version, failed and pending ones included.

    A scan, since the registry holds a handful of modules and the listing is an
    operator view. Modules sort by address and versions newest first.
    """
    resolved = settings or get_settings()
    grouped: dict[str, dict[str, Any]] = {}
    for row in repositories.registry(resolved).iter_scan(filter_expression=Attr("pk").begins_with(MODULE_PK_PREFIX)):
        module = grouped.setdefault(
            str(row["pk"]),
            {
                "namespace": str(row["namespace"]),
                "name": str(row["name"]),
                "provider": str(row["provider"]),
                "versions": [],
            },
        )
        module["versions"].append(_version_view(row))
    modules = []
    for pk in sorted(grouped):
        module = grouped[pk]
        module["source"] = f"{module['namespace']}/{module['name']}/{module['provider']}"
        module["versions"].sort(key=lambda view: version_order(view["version"]), reverse=True)
        modules.append(module)
    return modules


def upload_record(upload_id: str, *, settings: Settings | None = None) -> Optional[dict[str, Any]]:
    """The upload row for `upload_id`, or `None`."""
    resolved = settings or get_settings()
    return repositories.registry(resolved).get(upload_key(upload_id), consistent=True)


__all__ = [
    "DOWNLOAD_URL_TTL",
    "FAILED",
    "INCOMING_PREFIX",
    "MODULES_PREFIX",
    "PENDING",
    "PUBLISHED",
    "TRUSTED_CLAIMS",
    "UPLOAD_CONTENT_TYPE",
    "UPLOAD_ID_PATTERN",
    "UPLOAD_URL_TTL",
    "InvalidModuleName",
    "InvalidUploadToken",
    "ModuleNotFound",
    "RepositoryNotAllowed",
    "UnsupportedRef",
    "VersionAlreadyPublished",
    "allowlist",
    "download_url",
    "incoming_key",
    "issue_upload",
    "list_modules",
    "module_for",
    "module_key",
    "module_pk",
    "published_versions",
    "upload_id_for",
    "upload_record",
    "version_from_ref",
    "version_order",
    "version_sk",
    "verify_token",
]
