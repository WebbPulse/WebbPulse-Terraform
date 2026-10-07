"""State versions and outputs for `tfe.v2`, read from and written to the runner's state object.

A state version is one S3 object version of `workspaces/<id>/terraform.tfstate`, as it
is everywhere else in the plane, so the CLI's state commands and a run's engine read and
write one object with no second history. HCP ids carry no workspace, so a state version
id is `sv-<workspace ULID><S3 VersionId>` and an output id `wsout-` plus the URL safe
base64 of the workspace, version and output name, each resolving with no lookup table.

Unlike `state_versions`, this module hands output values back, because `terraform
output` is reading them on purpose; every route here sits behind `state:download`.
A write is accepted only from the subject holding the CLI lock (`locks.lock_holder`),
which the engine also honours, and keeps Terraform's lineage and serial rules.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, Final

from ...common.composition.settings import Settings, get_settings
from ...common.workspaces.reads import get_workspace
from . import locks
from .state_versions import (
    METADATA_READ_CEILING,
    STATE_CONTENT_TYPE,
    StateBucketMissing,
    StateVersionNotFound,
    get_state_version,
    state_key,
)

STATE_VERSION_PREFIX: Final = "sv-"
"""What every `tfe.v2` state version id starts with."""

OUTPUT_PREFIX: Final = "wsout-"
"""What every `tfe.v2` state version output id starts with, as HCP's do."""

ULID_LENGTH: Final = 26
"""A workspace id's ULID length, which is what lets a state version id split unambiguously."""

STATE_CREATE_CEILING: Final = 4 * 1024 * 1024
"""The largest state accepted inline: base64 of it still fits Lambda's 6 MB request."""


class StateRejected(Exception):
    """The state cannot be written as sent: a 422 the CLI shows verbatim."""


class StateConflict(Exception):
    """The state would overwrite newer or unrelated state, or the caller lacks the lock: a 409."""


class OutputNotFound(Exception):
    """No such output on that state version."""


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
    )


def _bucket(settings: Settings) -> str:
    """The state bucket, or `StateBucketMissing`."""
    if not settings.STATE_BUCKET:
        raise StateBucketMissing("STATE_BUCKET is unset, so there is no state.")
    return settings.STATE_BUCKET


def _missing(error: Exception) -> bool:
    """Whether a botocore `ClientError` means the object or version is not there."""
    response = getattr(error, "response", {}) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    return status in (400, 404) or code in ("404", "NoSuchKey", "NoSuchVersion", "NotFound", "InvalidArgument")


def state_version_id(workspace_id: str, version_id: str) -> str:
    """The `sv-` id for one S3 version of a workspace's state."""
    return f"{STATE_VERSION_PREFIX}{workspace_id.removeprefix('ws-')}{version_id}"


def parse_state_version_id(value: str) -> tuple[str, str]:
    """The workspace id and S3 version id an `sv-` id names, or `StateVersionNotFound`."""
    body = value.removeprefix(STATE_VERSION_PREFIX)
    if body == value or len(body) <= ULID_LENGTH:
        raise StateVersionNotFound(value)
    return f"ws-{body[:ULID_LENGTH]}", body[ULID_LENGTH:]


def output_id(workspace_id: str, version_id: str, name: str) -> str:
    """The `wsout-` id for one output of one state version."""
    raw = json.dumps([workspace_id, version_id, name], separators=(",", ":")).encode()
    return OUTPUT_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_output_id(value: str) -> tuple[str, str, str]:
    """The workspace, version and output name a `wsout-` id names, or `OutputNotFound`."""
    body = value.removeprefix(OUTPUT_PREFIX)
    if body == value:
        raise OutputNotFound(value)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, binascii.Error) as error:
        raise OutputNotFound(value) from error
    if not (isinstance(decoded, list) and len(decoded) == 3 and all(isinstance(part, str) for part in decoded)):
        raise OutputNotFound(value)
    return decoded[0], decoded[1], decoded[2]


def current_version_id(workspace_id: str, *, settings: Settings | None = None) -> str | None:
    """The S3 version id of the workspace's current state, or None when it has none."""
    from botocore.exceptions import ClientError

    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    try:
        head = _s3(resolved).head_object(Bucket=_bucket(resolved), Key=state_key(workspace_id))
    except ClientError as error:
        if _missing(error):
            return None
        raise
    version = head.get("VersionId")
    return str(version) if version else None


def describe(workspace_id: str, version_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One state version's metadata, as `state_versions.get_state_version` reads it."""
    return get_state_version(workspace_id, version_id, settings=settings)


def _read(workspace_id: str, version_id: str | None, settings: Settings) -> tuple[str, bytes] | None:
    """One version's id and bytes, the current one when `version_id` is None, or None when absent.

    A body over `METADATA_READ_CEILING` is returned truncated by one byte past the
    ceiling, which callers treat as unreadable.
    """
    from botocore.exceptions import ClientError

    kwargs: dict[str, Any] = {"Bucket": _bucket(settings), "Key": state_key(workspace_id)}
    if version_id is not None:
        kwargs["VersionId"] = version_id
    try:
        obj = _s3(settings).get_object(**kwargs)
    except ClientError as error:
        if _missing(error):
            return None
        raise
    with obj["Body"] as body:
        content = body.read(METADATA_READ_CEILING + 1)
    return str(obj.get("VersionId") or version_id or ""), content


def _parse(content: bytes) -> dict[str, Any] | None:
    """A state body parsed, or None when it is too large or not a JSON object."""
    if len(content) > METADATA_READ_CEILING:
        return None
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _value_type(value: Any) -> str:
    """HCP's coarse `type` for an output value, from its JSON shape."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def outputs(
    workspace_id: str, version_id: str, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Every root output of one state version: id, name, sensitive, type, detailed type and value.

    The detailed type is the cty type the state recorded, which is what lets the CLI
    decode a value without reading the whole state.

    Raises:
        StateVersionNotFound: No such state version on this workspace.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    read = _read(workspace_id, version_id, resolved)
    if read is None:
        raise StateVersionNotFound(version_id)
    parsed = _parse(read[1]) or {}
    found = parsed.get("outputs")
    rendered = []
    for name, output in sorted((found if isinstance(found, dict) else {}).items()):
        if not isinstance(output, dict):
            continue
        value = output.get("value")
        rendered.append(
            {
                "id": output_id(workspace_id, version_id, str(name)),
                "name": str(name),
                "sensitive": bool(output.get("sensitive", False)),
                "type": _value_type(value),
                "detailed_type": output.get("type"),
                "value": value,
            }
        )
    return rendered


def output(output_ref: str, *, settings: Settings | None = None) -> tuple[str, str, dict[str, Any]]:
    """One output by its `wsout-` id, with the workspace and version it belongs to.

    Raises:
        OutputNotFound: The id does not name an output of a state version.
    """
    workspace_id, version_id, name = parse_output_id(output_ref)
    try:
        found = outputs(workspace_id, version_id, settings=settings)
    except StateVersionNotFound as error:
        raise OutputNotFound(output_ref) from error
    for item in found:
        if item["name"] == name:
            return workspace_id, version_id, item
    raise OutputNotFound(output_ref)


def _decode(encoded: Any) -> bytes:
    """The state bytes out of the request's base64, or `StateRejected`."""
    if not isinstance(encoded, str) or not encoded:
        raise StateRejected("param is missing or the value is empty: state")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise StateRejected("The state is not valid base64.") from error
    if len(content) > STATE_CREATE_CEILING:
        raise StateRejected("The state is larger than the 4 MB an inline state version may carry.")
    return content


def create(
    workspace_id: str,
    subject: str,
    attributes: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> str:
    """Write a new state version from the CLI and return its S3 version id.

    The checks are Terraform's own remote state rules: the bytes match `md5`, their
    serial and lineage match the attributes, the lineage is the current state's unless
    there is none or `force` is set, and the serial moves forward unless `force` is set,
    an equal serial passing only for identical bytes. The caller must hold the CLI lock,
    so no run's engine is writing at the same time.

    Raises:
        StateRejected: The request is malformed or the bytes do not match it.
        StateConflict: The caller lacks the lock, or the state would go backwards.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    content = _decode(attributes.get("state"))

    md5 = attributes.get("md5")
    if not isinstance(md5, str) or hashlib.md5(content, usedforsecurity=False).hexdigest() != md5.lower():
        raise StateRejected("The state does not match its md5.")
    serial = attributes.get("serial")
    if type(serial) is not int or serial < 0:
        raise StateRejected("The serial must be a whole number.")
    lineage = attributes.get("lineage")
    if lineage is not None and not isinstance(lineage, str):
        raise StateRejected("The lineage must be a string.")
    force = attributes.get("force") is True

    parsed = _parse(content)
    if parsed is None:
        raise StateRejected("The state is not a Terraform state document.")
    if parsed.get("serial") != serial:
        raise StateRejected("The state's serial does not match the serial sent.")
    if lineage and parsed.get("lineage") != lineage:
        raise StateRejected("The state's lineage does not match the lineage sent.")

    if locks.lock_holder(workspace_id, settings=resolved) != subject:
        raise StateConflict("The workspace must be locked by you before its state can be written.")

    current = _read(workspace_id, None, resolved)
    if current is not None and not force:
        current_version, current_content = current
        current_state = _parse(current_content)
        if current_state is None:
            raise StateConflict("The current state cannot be read, so only a forced write may replace it.")
        if current_state.get("lineage") != parsed.get("lineage"):
            raise StateConflict("The state's lineage differs from the current state's. Use -force to replace it.")
        current_serial = current_state.get("serial")
        if type(current_serial) is int:
            if serial < current_serial:
                raise StateConflict(f"The state's serial {serial} is older than the current serial {current_serial}.")
            if serial == current_serial:
                if content == current_content:
                    return current_version
                raise StateConflict(f"The state differs from the current state at the same serial {serial}.")

    encryption: dict[str, str] = {"ServerSideEncryption": "aws:kms"}
    if resolved.STATE_KMS_KEY_ARN:
        encryption["SSEKMSKeyId"] = resolved.STATE_KMS_KEY_ARN
    written = _s3(resolved).put_object(
        Bucket=_bucket(resolved),
        Key=state_key(workspace_id),
        Body=content,
        ContentType=STATE_CONTENT_TYPE,
        **encryption,
    )
    return str(written.get("VersionId") or "")


__all__ = [
    "OUTPUT_PREFIX",
    "STATE_VERSION_PREFIX",
    "OutputNotFound",
    "StateConflict",
    "StateRejected",
    "create",
    "current_version_id",
    "describe",
    "output",
    "output_id",
    "outputs",
    "parse_output_id",
    "parse_state_version_id",
    "state_version_id",
]
