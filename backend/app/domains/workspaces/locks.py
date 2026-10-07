"""Workspace locks for `tfe.v2`, held in the S3 backend's own native lockfile.

A run's engine locks state by writing `terraform.tfstate.tflock` beside the state
with `If-None-Match: *`, and waits on it with `-lock-timeout`. A CLI lock here is the
same object written the same way, so the CLI and a run exclude each other through one
lock with no second record to drift: whichever write lands first holds it.

A lockfile this module wrote names its holder in `LOCK_OWNER_FIELD`, the caller's
subject. One without that field was written by a run's engine, which is how unlock
tells "is locked by User" from "is locked by Run", the two phrases go-tfe maps.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from webbpulse.dynamodb import now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.runs.workspace_runs import RunStillActive, require_no_active_run
from ...common.workspaces.reads import get_workspace
from .state_versions import StateBucketMissing, state_key

LOCK_SUFFIX: Final = ".tflock"
"""What the S3 backend appends to the state key for its native lockfile."""

LOCK_OWNER_FIELD: Final = "WebbPulseLockedBy"
"""The lockfile field naming the subject that took a CLI lock. The engine ignores it."""

LOCK_OPERATION: Final = "tfe.v2 workspace lock"
"""The `Operation` a run's engine prints when it finds the workspace locked from the CLI."""

LOCK_READ_CEILING: Final = 64 * 1024
"""The most of a lockfile read back. A lock body is a few hundred bytes."""


class WorkspaceLocked(Exception):
    """The lockfile is already there, so the workspace is locked."""


class WorkspaceNotLocked(Exception):
    """There is no lockfile to remove."""


class LockedByRun(Exception):
    """A run holds the lock, or is executing, so the CLI may not take or drop it."""


class LockedByOther(Exception):
    """Another subject took the CLI lock, so this caller may not unlock it."""

    def __init__(self, holder: str) -> None:
        """Keep the holder, for the message the CLI shows."""
        super().__init__(holder)
        self.holder = holder


def lock_key(workspace_id: str) -> str:
    """The lockfile key, the one the runner's S3 backend writes for this workspace."""
    return f"{state_key(workspace_id)}{LOCK_SUFFIX}"


def _bucket(settings: Settings) -> str:
    """The state bucket, or `StateBucketMissing`."""
    if not settings.STATE_BUCKET:
        raise StateBucketMissing("STATE_BUCKET is unset, so no workspace can be locked.")
    return settings.STATE_BUCKET


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
    )


def _error_code(error: Exception) -> str:
    """The S3 error code and HTTP status of a botocore `ClientError`, joined."""
    response = getattr(error, "response", {}) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    status = str(response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return f"{code}:{status}"


def read_lock(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """The lockfile's body, `{}` when it is there but unreadable, or None when absent."""
    from botocore.exceptions import ClientError

    resolved = settings or get_settings()
    try:
        response = _s3(resolved).get_object(Bucket=_bucket(resolved), Key=lock_key(workspace_id))
    except ClientError as error:
        code = _error_code(error)
        if code.startswith(("NoSuchKey:", "404:")) or code.endswith(":404"):
            return None
        raise
    body = response["Body"].read(LOCK_READ_CEILING)
    try:
        parsed = json.loads(body)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def is_locked(workspace_id: str, *, settings: Settings | None = None) -> bool:
    """Whether the workspace's lockfile is there, which is what `locked` renders."""
    from botocore.exceptions import ClientError

    resolved = settings or get_settings()
    if not resolved.STATE_BUCKET:
        return False
    try:
        _s3(resolved).head_object(Bucket=resolved.STATE_BUCKET, Key=lock_key(workspace_id))
    except ClientError as error:
        code = _error_code(error)
        if code.startswith(("NoSuchKey:", "404:", "NotFound:")) or code.endswith(":404"):
            return False
        raise
    return True


def lock(workspace_id: str, subject: str, reason: str, *, settings: Settings | None = None) -> None:
    """Write the lockfile for `subject`, refused while a run is going or another lock holds.

    The write is conditional on no object being there, the same condition the engine
    writes under, so a run and the CLI racing for the lock cannot both win.
    """
    from botocore.exceptions import ClientError

    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    try:
        require_no_active_run(workspace_id, settings=resolved)
    except RunStillActive as error:
        raise LockedByRun(error.run_id) from error

    bucket = _bucket(resolved)
    key = lock_key(workspace_id)
    body = {
        "ID": str(uuid.uuid4()),
        "Operation": LOCK_OPERATION,
        "Info": reason,
        "Who": subject,
        "Version": "",
        "Created": now_iso(),
        "Path": f"{bucket}/{key}",
        LOCK_OWNER_FIELD: subject,
    }
    encryption: dict[str, str] = {"ServerSideEncryption": "aws:kms"}
    if resolved.STATE_KMS_KEY_ARN:
        encryption["SSEKMSKeyId"] = resolved.STATE_KMS_KEY_ARN
    try:
        _s3(resolved).put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(body).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
            **encryption,
        )
    except ClientError as error:
        code = _error_code(error)
        if code.startswith(("PreconditionFailed:", "ConditionalRequestConflict:")) or code.endswith((":412", ":409")):
            raise WorkspaceLocked(workspace_id) from error
        raise


def _delete(workspace_id: str, settings: Settings) -> None:
    """Remove the lockfile. The bucket is versioned, so this leaves a delete marker."""
    _s3(settings).delete_object(Bucket=_bucket(settings), Key=lock_key(workspace_id))


def unlock(workspace_id: str, subject: str, *, settings: Settings | None = None) -> None:
    """Remove a CLI lock this subject holds.

    A lock written by a run's engine, or by another subject, is refused: only the
    holder unlocks, and `force-unlock` is the way past a lock someone else left.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    held = read_lock(workspace_id, settings=resolved)
    if held is None:
        raise WorkspaceNotLocked(workspace_id)
    holder = held.get(LOCK_OWNER_FIELD)
    if not holder:
        raise LockedByRun(str(held.get("ID") or ""))
    if str(holder) != subject:
        raise LockedByOther(str(holder))
    _delete(workspace_id, resolved)


def force_unlock(workspace_id: str, *, settings: Settings | None = None) -> None:
    """Remove whatever lockfile is there, unless a run is still going.

    This is the way past a lock a crashed runner or another person left. A run that is
    still going may be holding its lock right now, so that is refused rather than
    letting two writers at the state.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    if read_lock(workspace_id, settings=resolved) is None:
        raise WorkspaceNotLocked(workspace_id)
    try:
        require_no_active_run(workspace_id, settings=resolved)
    except RunStillActive as error:
        raise LockedByRun(error.run_id) from error
    _delete(workspace_id, resolved)


def lock_holder(workspace_id: str, *, settings: Settings | None = None) -> str | None:
    """The subject holding a CLI lock on the workspace, or None for no lock or a run's."""
    held = read_lock(workspace_id, settings=settings)
    if not held:
        return None
    holder = held.get(LOCK_OWNER_FIELD)
    return str(holder) if holder else None
