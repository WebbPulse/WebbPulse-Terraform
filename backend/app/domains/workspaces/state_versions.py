"""Reading a workspace's Terraform state history out of S3 object versioning.

The state bucket is versioned, so S3 already holds every state a run has written
and there is no second copy to keep: a state version here is one S3 object
version of `workspaces/<id>/terraform.tfstate`, named by its `VersionId`. Nothing
in this module writes, and no row records a state version, because a row would be
a parallel history that drifts from the bucket the moment a lifecycle rule
expires a version.

Terraform writes state through its own S3 backend, so the control plane never
sees the write and cannot stamp user metadata on the object. The serial and the
terraform version therefore have to come out of the state body, which means a
decrypt, which is why `_describe` reads the body only when a caller asked for one
version's metadata and never while listing a page.

What a version's metadata carries is deliberately narrow. A state body holds
resource attributes and outputs in plaintext, routinely including passwords,
private keys and tokens; only `serial`, `terraform_version` and `lineage` are
lifted out of it, and the resource and output bodies are never read into a
response. `_summary` is what enforces that: it takes the parsed body and returns
those three fields, so a future edit that wants to surface more has to change
this module rather than a caller.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Final, Optional

from ...common.composition.settings import Settings, get_settings
from ...common.workspaces.reads import get_workspace

STATE_CONTENT_TYPE: Final = "application/json"
"""What S3 returns the object as. Signed into a download URL so the bytes cannot
be served back under a type a browser would render inline."""

DOWNLOAD_EXPIRES_IN: Final = 60
"""Seconds a state download URL stays valid.

A presigned URL is a bearer credential that carries no identity, so the window is
the only thing limiting who can spend it once it leaves the response. Sixty
seconds is enough for a client that asked for the URL to follow it immediately
and short enough that one pasted into a chat log or captured in a proxy is dead
before it is read. It is deliberately far below the hour the run bundle uses:
that URL is fetched by a machine that already holds a run token, this one is
handed to a browser.
"""

MAX_PAGE_SIZE: Final = 100
"""The most versions one page returns, which bounds the S3 call behind it."""

DEFAULT_PAGE_SIZE: Final = 20
"""The page size a caller that names none gets."""

STATE_SUFFIX: Final = "/terraform.tfstate"
"""The exact key ending a state object has.

Listing a workspace's prefix returns its `.tflock` objects too, because the S3
backend's native lock is a sibling key under the same prefix. Matching the whole
suffix rather than the prefix alone is what keeps a lock's version history out of
a state history, and `.tflock` does not end in this string.
"""

METADATA_READ_CEILING: Final = 64 * 1024 * 1024
"""The largest state body this module will parse for its serial.

A state file is JSON that has to be read whole to be parsed, so an unbounded read
would let one very large state decide the function's memory. A body over the
ceiling still lists and still downloads; only its serial reads as unknown.
"""


class StateVersionNotFound(Exception):
    """No such state version on this workspace, or it is not a state object."""


class StateBucketMissing(Exception):
    """The deployment set no state bucket, so there is no history to read."""


class InvalidPageToken(Exception):
    """The cursor is malformed or belongs to a different state key."""


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
    )


def _presigner(settings: Settings) -> Any:
    """An S3 client that signs with SigV4, for the one version pinned URL.

    `webbpulse.storage.presigned_get` is the normal way to mint a download here
    and is used everywhere else in this codebase, but it signs a bucket and a key
    alone and exposes no way to put `VersionId` inside the signature. A URL
    without it resolves to whichever state is current when it is spent, so asking
    for an old version would hand back today's state: wrong, and wrong in the
    direction that over exposes. Signing directly is the narrow workaround until
    `presigned_get` takes extra parameters upstream, and SigV4 is pinned for the
    same reason the package pins it.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
        config=Config(signature_version="s3v4"),
    )


def state_key(workspace_id: str) -> str:
    """The state object key for one workspace.

    The same layout the runner's backend block is built from, kept here so a
    reader and a writer cannot drift apart.
    """
    return f"workspaces/{workspace_id}{STATE_SUFFIX}"


def _bucket(settings: Settings) -> str:
    """The state bucket, or `StateBucketMissing`."""
    if not settings.STATE_BUCKET:
        raise StateBucketMissing("STATE_BUCKET is unset, so no state history can be read.")
    return settings.STATE_BUCKET


def encode_page_token(key_marker: str, version_marker: str) -> str:
    """Render S3's two continuation markers as one opaque token.

    S3 continues a version listing with a pair, and a caller should not have to
    carry two query parameters or learn what they mean, so the pair travels as
    one base64 blob. It is opaque rather than signed on purpose: it names a key
    this caller was already authorized for and steering it elsewhere cannot widen
    what they reach, because the listing is re-scoped to this workspace's key on
    every request.
    """
    return base64.urlsafe_b64encode(json.dumps([key_marker, version_marker]).encode()).decode()


def decode_page_token(token: str) -> tuple[str, str]:
    """Reject malformed cursors instead of silently repeating the first page."""
    try:
        raw = json.loads(base64.b64decode(token.encode(), altchars=b"-_", validate=True).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError, RecursionError) as error:
        raise InvalidPageToken from error
    if not isinstance(raw, list) or len(raw) != 2:
        raise InvalidPageToken
    first, second = raw
    if not isinstance(first, str) or not isinstance(second, str) or not first or not second:
        raise InvalidPageToken
    return first, second


def _render(workspace_id: str, version: dict[str, Any]) -> dict[str, Any]:
    """One S3 object version as the listing renders it.

    Everything here comes from the version listing, which is metadata alone: no
    state body is read to build a page, so listing a thousand versions decrypts
    nothing and costs no KMS call.
    """
    last_modified = version.get("LastModified")
    return {
        "workspace_id": workspace_id,
        "state_version_id": str(version.get("VersionId", "")),
        "created_at": last_modified.isoformat() if last_modified is not None else "",
        "size_bytes": int(version.get("Size", 0)),
        "is_current": bool(version.get("IsLatest", False)),
    }


def list_state_versions(
    workspace_id: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_token: Optional[str] = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One page of a workspace's state versions, newest first.

    A workspace that has never run has no object and so no versions, which is an
    empty page rather than a 404: the workspace exists and its history is simply
    empty.

    S3 returns a key's versions newest first already, and the listing is filtered
    to the one state key, so a `.tflock` version and any other object under the
    prefix are dropped before a caller sees them.

    Raises:
        WorkspaceNotFound: No such workspace.
        StateBucketMissing: The deployment set no state bucket.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    bucket = _bucket(resolved)

    key = state_key(workspace_id)
    bounded = max(1, min(int(page_size), MAX_PAGE_SIZE))

    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": key, "MaxKeys": bounded}
    if page_token:
        key_marker, version_marker = decode_page_token(page_token)
        if key_marker != key:
            raise InvalidPageToken
        kwargs["KeyMarker"] = key_marker
        kwargs["VersionIdMarker"] = version_marker

    from botocore.exceptions import ClientError

    try:
        response = _s3(resolved).list_object_versions(**kwargs)
    except ClientError as error:
        if page_token and error.response.get("Error", {}).get("Code") == "InvalidArgument":
            raise InvalidPageToken from error
        raise

    items = [
        _render(workspace_id, version) for version in response.get("Versions", []) if str(version.get("Key", "")) == key
    ]

    next_token: Optional[str] = None
    if response.get("IsTruncated"):
        next_key = str(response.get("NextKeyMarker", ""))
        next_version = str(response.get("NextVersionIdMarker", ""))
        if next_key == key and next_version:
            next_token = encode_page_token(next_key, next_version)

    return {"items": items, "next_page_token": next_token}


def _head(workspace_id: str, state_version_id: str, *, settings: Settings) -> dict[str, Any]:
    """The S3 metadata for one state version, or `StateVersionNotFound`.

    A version id naming an object in another workspace cannot be reached: the
    HEAD names this workspace's key, so an id lifted from another workspace's
    history answers 404 here rather than returning that workspace's object.
    """
    from botocore.exceptions import ClientError

    try:
        return dict(
            _s3(settings).head_object(
                Bucket=_bucket(settings),
                Key=state_key(workspace_id),
                VersionId=state_version_id,
            )
        )
    except ClientError as error:
        status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        code = str(error.response.get("Error", {}).get("Code", ""))
        if status in (400, 404, 405) or code in ("404", "NoSuchKey", "NotFound", "NoSuchVersion", "InvalidArgument"):
            raise StateVersionNotFound(state_version_id) from error
        raise


def _summary(body: bytes) -> dict[str, Any]:
    """The three non sensitive fields lifted out of a state body.

    This is the boundary the module is built around. A state body holds every
    resource attribute and every output in plaintext, so nothing but the serial,
    the terraform version and the lineage leaves this function, and a body that
    does not parse yields empty values rather than raising: unreadable metadata
    is not a reason to fail a metadata read that is otherwise correct.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {"serial": None, "terraform_version": None, "lineage": None}
    if not isinstance(parsed, dict):
        return {"serial": None, "terraform_version": None, "lineage": None}

    serial = parsed.get("serial")
    terraform_version = parsed.get("terraform_version")
    lineage = parsed.get("lineage")
    return {
        "serial": serial if type(serial) is int else None,
        "terraform_version": str(terraform_version) if isinstance(terraform_version, str) else None,
        "lineage": str(lineage) if isinstance(lineage, str) else None,
    }


def get_state_version(
    workspace_id: str,
    state_version_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One state version's metadata, including its serial and terraform version.

    The serial and the terraform version are only in the state body, because
    Terraform writes the object itself and the control plane has no chance to
    stamp them as user metadata. Reading them costs one GET and one decrypt, so
    it happens here and never while listing.

    Raises:
        WorkspaceNotFound: No such workspace.
        StateVersionNotFound: No such state version on this workspace.
        StateBucketMissing: The deployment set no state bucket.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)

    head = _head(workspace_id, state_version_id, settings=resolved)
    last_modified = head.get("LastModified")
    size_bytes = int(head.get("ContentLength", 0))

    rendered: dict[str, Any] = {
        "workspace_id": workspace_id,
        "state_version_id": state_version_id,
        "created_at": last_modified.isoformat() if last_modified is not None else "",
        "size_bytes": size_bytes,
        "is_current": False,
        "serial": None,
        "terraform_version": None,
        "lineage": None,
        "run_id": None,
    }

    if size_bytes and size_bytes <= METADATA_READ_CEILING:
        from botocore.exceptions import ClientError

        try:
            obj = _s3(resolved).get_object(
                Bucket=_bucket(resolved),
                Key=state_key(workspace_id),
                VersionId=state_version_id,
            )
            with obj["Body"] as body:
                content = body.read(METADATA_READ_CEILING + 1)
            if len(content) <= METADATA_READ_CEILING:
                rendered.update(_summary(content))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "NoSuchVersion"):
                raise StateVersionNotFound(state_version_id) from error
            raise

    current = _current_version_id(workspace_id, settings=resolved)
    rendered["is_current"] = current is not None and current == state_version_id
    return rendered


def _current_version_id(workspace_id: str, *, settings: Settings) -> Optional[str]:
    """The version id of the workspace's current state, or `None` when it has none."""
    from botocore.exceptions import ClientError

    try:
        head = _s3(settings).head_object(Bucket=_bucket(settings), Key=state_key(workspace_id))
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        raise
    version_id = head.get("VersionId")
    return str(version_id) if version_id else None


def state_version_download(
    workspace_id: str,
    state_version_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Mint a short lived URL for one state version's raw bytes.

    The bytes never pass through this function. A state file is the most
    sensitive object the product holds and can be tens of megabytes, so
    streaming it through Lambda would buffer plaintext state in the function's
    memory and in whatever sits in front of it, for no gain over letting S3 serve
    it directly.

    Every check happens before anything is signed. The workspace is read first,
    which is what raises for a workspace that is not there, and the HEAD that
    follows both proves the version exists and pins it to this workspace's key.
    Only then is a URL minted, and it authorizes exactly one bucket, one key and
    one version id: a holder cannot walk it to another workspace's state, because
    the key is inside the signature. It expires in `DOWNLOAD_EXPIRES_IN` seconds.

    Raises:
        WorkspaceNotFound: No such workspace.
        StateVersionNotFound: No such state version on this workspace.
        StateBucketMissing: The deployment set no state bucket.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    head = _head(workspace_id, state_version_id, settings=resolved)

    url = _presigner(resolved).generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": _bucket(resolved),
            "Key": state_key(workspace_id),
            "VersionId": state_version_id,
            "ResponseContentType": STATE_CONTENT_TYPE,
            "ResponseCacheControl": "no-store",
            "ResponseContentDisposition": (f'attachment; filename="{workspace_id}-{state_version_id}.tfstate"'),
        },
        ExpiresIn=DOWNLOAD_EXPIRES_IN,
        HttpMethod="GET",
    )

    return {
        "workspace_id": workspace_id,
        "state_version_id": state_version_id,
        "download_url": url,
        "expires_in": DOWNLOAD_EXPIRES_IN,
        "size_bytes": int(head.get("ContentLength", 0)),
    }


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DOWNLOAD_EXPIRES_IN",
    "MAX_PAGE_SIZE",
    "InvalidPageToken",
    "StateBucketMissing",
    "StateVersionNotFound",
    "get_state_version",
    "list_state_versions",
    "state_key",
    "state_version_download",
]
