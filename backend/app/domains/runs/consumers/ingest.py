"""The ingest consumer: turns an uploaded tarball into runs on the bound workspaces.

An EventBridge rule on the artifacts bucket's `Object Created` events under
`ingest/` stamps `config_ingested` on the message. The object key names an upload
id, and everything this consumer trusts about the upload, the repository, the
event, the branch, the pull request and the commit, comes from the ingest record
the upload route wrote under that id from a verified GitHub token. Nothing is read
from the object's metadata, and a key with no record is dropped.

Delivery is at least once, and the workflow's PUT retries, so one key can fire
twice. Every id this writes is derived from the upload id and the workspace id,
so a redelivery finds its own config version and its own run already there and
does nothing new.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import re
import tarfile
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from ....common.db.tables import RUNS_BY_WORKSPACE_INDEX
from ....common.workspaces import reads as workspace_reads
from ....common.workspaces import vcs as workspace_vcs
from .. import service
from ..vcs import PULL_REQUEST_EVENT, PUSH_EVENT, UPLOAD_ID_PATTERN, crockford

_log = logging.getLogger(__name__)

INGEST_KIND = "config_ingested"
"""The `kind` the EventBridge rule's input transformer stamps on the message."""

CHANGED_PATHS_MEMBER = ".webbpulse/changed-paths.txt"
"""Where the workflow writes the paths the commit changed, one per line. A `*`
line, or no such file, means every path."""

MAX_CHANGED_PATHS_BYTES = 5_000_000
EVERYTHING = "*"

_INGEST_KEY = re.compile(rf"^ingest/(?P<upload_id>{UPLOAD_ID_PATTERN})\.tar\.gz$")

SOURCE_PUSH = "vcs_push"
SOURCE_PR = "vcs_pr"

SUPERSEDE_CANCEL_STATUSES = frozenset({"pending"})
SUPERSEDE_DISCARD_STATUSES = frozenset({"awaiting_confirmation"})


class MalformedIngest(Exception):
    """The message body is not an object created event this consumer can read."""


def parse_body(record: Mapping[str, Any]) -> tuple[str, str, int]:
    """The bucket, key and size one queue record carries.

    Raises:
        MalformedIngest: The body is not JSON, not a `config_ingested`, or lacks a
            bucket, key or size, so it parks on the dead letter queue.
    """
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise MalformedIngest("The record carries no body.")
    try:
        body = json.loads(raw)
    except ValueError as error:
        raise MalformedIngest("The body is not JSON.") from error
    if not isinstance(body, Mapping):
        raise MalformedIngest("The body is not a JSON object.")
    kind = str(body.get("kind", ""))
    if kind != INGEST_KIND:
        raise MalformedIngest(f"The body is a {kind or 'kindless'} message, not a {INGEST_KIND}.")
    bucket = body.get("bucket")
    key = body.get("key")
    size = body.get("size")
    if not isinstance(bucket, str) or not isinstance(key, str) or not isinstance(size, int):
        raise MalformedIngest("The body lacks a bucket, a key or a size.")
    return bucket, key, size


def upload_millis(upload: Mapping[str, Any]) -> int:
    """When the upload was recorded, in epoch milliseconds."""
    if upload.get("created_at_ms") is not None:
        return int(upload["created_at_ms"])
    return int(datetime.fromisoformat(str(upload["created_at"])).timestamp() * 1000)


def deterministic_id(prefix: str, millis: int, seed: str) -> str:
    """A ULID shaped id whose time is the upload's and whose rest is derived from `seed`.

    The same upload and workspace always get the same id, which is what makes a
    redelivery idempotent, and ids still sort by upload time, which is what the
    supersede check compares.
    """
    millis &= (1 << 48) - 1
    randomness = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:10], "big")
    value = (millis << 80) | randomness
    return prefix + crockford((value << 6).to_bytes(17, "big"))


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def read_changed_paths(bucket: str, key: str, *, settings: Settings) -> Optional[list[str]]:
    """The changed paths the tarball's `.webbpulse/changed-paths.txt` lists.

    Streams the archive and stops at that member. `None` means every path: the
    file is absent, too large, or holds a `*` line.
    """
    body = _s3(settings).get_object(Bucket=bucket, Key=key)["Body"]
    try:
        with tarfile.open(fileobj=body, mode="r|gz") as archive:
            for member in archive:
                if member.name.removeprefix("./") != CHANGED_PATHS_MEMBER:
                    continue
                if not member.isfile() or member.size > MAX_CHANGED_PATHS_BYTES:
                    return None
                handle = archive.extractfile(member)
                if handle is None:
                    return None
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
                paths = [line.strip().removeprefix("./") for line in lines if line.strip()]
                return None if EVERYTHING in paths else paths
    except (tarfile.TarError, EOFError, OSError):
        return None
    finally:
        body.close()
    return None


def trigger_patterns(workspace: Mapping[str, Any]) -> list[str]:
    """The workspace's patterns, or everything under its working directory."""
    patterns = [str(pattern) for pattern in workspace.get("trigger_patterns") or [] if str(pattern).strip()]
    if patterns:
        return patterns
    directory = str(workspace.get("working_directory", "") or "").strip().strip("/").removeprefix("./")
    return [f"{directory}/**"] if directory else ["**"]


def paths_match(patterns: Iterable[str], paths: Optional[list[str]]) -> bool:
    """Whether any changed path matches any pattern. `None` paths match everything."""
    if paths is None:
        return True
    compiled = [
        re.compile(glob.translate(pattern.strip().lstrip("/"), recursive=True, include_hidden=True))
        for pattern in patterns
    ]
    return any(regex.match(path) for regex in compiled for path in paths)


def _eligible(workspace: Mapping[str, Any], upload: Mapping[str, Any]) -> bool:
    """Whether this upload's event is one this workspace runs on."""
    if upload["event"] == PUSH_EVENT:
        tracked = workspace.get("tracked_branch")
        return bool(tracked) and str(tracked) == str(upload.get("branch", ""))
    if upload["event"] == PULL_REQUEST_EVENT:
        return bool(workspace.get("speculative_plans", True))
    return False


def _ensure_config_version(
    workspace_id: str,
    config_version_id: str,
    upload: Mapping[str, Any],
    *,
    settings: Settings,
) -> None:
    """Copy the tarball to the workspace's config key and record it `uploaded`.

    The row is written after the copy, so an `uploaded` row always has its object,
    and an existing row means both are already done.
    """
    table = repositories.config_versions(settings)
    if table.get({"workspace_id": workspace_id, "config_version_id": config_version_id}, consistent=True):
        return
    key = workspace_reads.config_key(workspace_id, config_version_id)
    _s3(settings).copy_object(
        Bucket=settings.ARTIFACTS_BUCKET,
        Key=key,
        CopySource={"Bucket": settings.ARTIFACTS_BUCKET, "Key": str(upload["key"])},
        ContentType="application/gzip",
        MetadataDirective="REPLACE",
    )
    item = {
        "config_version_id": config_version_id,
        "workspace_id": workspace_id,
        "key": key,
        "status": "uploaded",
        "size_bytes": int(upload["size_bytes"]),
        "source": "vcs",
        "upload_id": str(upload["upload_id"]),
        "created_at": str(upload["created_at"]),
    }
    try:
        table.put(item, condition=Attr("config_version_id").not_exists())
    except ConditionFailed:
        pass


def _same_source(run: Mapping[str, Any], source: str, upload: Mapping[str, Any]) -> bool:
    """Whether an existing run came from the same stream of uploads as this one:
    pushes to the same branch, or the same pull request of the same repository."""
    if str(run.get("source", "api")) != source:
        return False
    vcs = run.get("vcs") or {}
    if str(vcs.get("repository_id", "")) != str(upload["repository_id"]):
        return False
    if source == SOURCE_PR:
        return int(vcs.get("pr_number", 0) or 0) == int(upload.get("pr_number", 0) or 0)
    return str(vcs.get("branch", "")) == str(upload.get("branch", ""))


def _supersede(workspace_id: str, run_id: str, source: str, upload: Mapping[str, Any], *, settings: Settings) -> bool:
    """End the older runs this upload replaces. Returns False when a newer one exists.

    An older pending run is cancelled and an older run awaiting confirmation is
    discarded. A run that already moved on is left alone, and one that a
    concurrent path ended first is not an error.
    """
    runs = repositories.runs(settings)
    for run in runs.iter_query(Key("workspace_id").eq(workspace_id), index_name=RUNS_BY_WORKSPACE_INDEX):
        other = str(run.get("run_id", ""))
        if other == run_id or not _same_source(run, source, upload):
            continue
        if other > run_id:
            return False
        status = str(run.get("status", ""))
        try:
            if status in SUPERSEDE_CANCEL_STATUSES:
                service.cancel_run(other, settings=settings)
            elif status in SUPERSEDE_DISCARD_STATUSES:
                service.discard_run(other, settings=settings)
            else:
                continue
        except (service.RunNotFound, service.RunNotCancellable, service.RunNotDiscardable):
            continue
        _log.info(
            "Superseded an older VCS run.",
            extra={"event": "runs.ingest.superseded", "run_id": other, "by": run_id, "status": status},
        )
    return True


def _resume(run_id: str, *, settings: Settings) -> None:
    """Start a run a previous delivery stored but never started."""
    run = service.get_run(run_id, settings=settings)
    if str(run.get("status", "")) == "pending" and not run.get("execution_arn") and not run.get("queued_behind"):
        service.start_run(run_id, settings=settings)


def _run_for_workspace(workspace: Mapping[str, Any], upload: Mapping[str, Any], *, settings: Settings) -> Optional[str]:
    """Create, or find already created, this upload's run on one workspace."""
    workspace_id = str(workspace["workspace_id"])
    seed = f"{upload['upload_id']}:{workspace_id}"
    millis = upload_millis(upload)
    config_version_id = deterministic_id("cv-", millis, f"cv:{seed}")
    run_id = deterministic_id(service.RUN_ID_PREFIX, millis, f"run:{seed}")
    is_pr = upload["event"] == PULL_REQUEST_EVENT
    source = SOURCE_PR if is_pr else SOURCE_PUSH

    try:
        service.get_run(run_id, settings=settings)
    except service.RunNotFound:
        pass
    else:
        _resume(run_id, settings=settings)
        return run_id

    _ensure_config_version(workspace_id, config_version_id, upload, settings=settings)
    if not _supersede(workspace_id, run_id, source, upload, settings=settings):
        _log.info(
            "Skipped an upload a newer one already superseded.",
            extra={"event": "runs.ingest.stale", "workspace_id": workspace_id, "upload_id": upload["upload_id"]},
        )
        return None

    sha = str(upload["sha"])
    if is_pr:
        shown = str(upload.get("head_sha") or sha)[:7]
        message = f"Pull request #{upload['pr_number']} at {shown}"
    else:
        message = f"Push of {sha[:7]} to {upload['branch']}"
    vcs = {
        "repo": upload["repo"],
        "repository_id": upload["repository_id"],
        "sha": sha,
        "ref": upload["ref"],
        "branch": upload.get("branch"),
        "pr_number": upload.get("pr_number"),
        "head_sha": upload.get("head_sha"),
        "base_sha": upload.get("base_sha"),
    }
    actor = {"kind": "vcs", "id": f"github:{upload['actor']}", "display_name": str(upload["actor"])}
    try:
        service.create_run(
            {
                "workspace_id": workspace_id,
                "config_version_id": config_version_id,
                "plan_only": is_pr,
                "message": message,
            },
            actor=actor,
            run_id=run_id,
            source=source,
            vcs=vcs,
            settings=settings,
        )
    except ConditionFailed:
        _resume(run_id, settings=settings)
    return run_id


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> list[str]:
    """Start this upload's runs on every bound workspace it triggers. Returns the run ids.

    A foreign bucket, a key outside the upload id shape, an expired or absent
    record and a size the record does not declare are logged and dropped. A
    workspace with no run role is skipped rather than failing its siblings.

    Raises:
        MalformedIngest: The body is not a usable object created event.
    """
    resolved = settings or get_settings()
    bucket, key, size = parse_body(record)
    match = _INGEST_KEY.match(key)
    if bucket != resolved.ARTIFACTS_BUCKET or match is None:
        _log.warning(
            "Ignoring an object outside the ingest key shape.",
            extra={"event": "runs.ingest.ignored", "bucket": bucket, "key": key},
        )
        return []
    upload_id = match.group("upload_id")
    upload = repositories.vcs_uploads(resolved).get({"upload_id": upload_id}, consistent=True)
    if upload is None:
        _log.warning(
            "Ignoring an upload with no ingest record.",
            extra={"event": "runs.ingest.unrecorded", "upload_id": upload_id},
        )
        return []
    if int(upload.get("size_bytes", -1)) != size:
        _log.warning(
            "Ignoring an object whose size the ingest record does not declare.",
            extra={"event": "runs.ingest.size_mismatch", "upload_id": upload_id, "size": size},
        )
        return []

    workspaces = [
        workspace
        for workspace in workspace_vcs.bound_workspaces(
            str(upload["repo"]), str(upload["repository_id"]), settings=resolved
        )
        if _eligible(workspace, upload)
    ]
    if not workspaces:
        _log.info(
            "No bound workspace runs on this upload.", extra={"event": "runs.ingest.no_match", "upload_id": upload_id}
        )
        return []

    paths = read_changed_paths(bucket, key, settings=resolved)
    started: list[str] = []
    for workspace in workspaces:
        workspace_id = str(workspace["workspace_id"])
        if not paths_match(trigger_patterns(workspace), paths):
            continue
        if not str(workspace.get("run_role_arn", "") or ""):
            _log.info(
                "Skipped a workspace with no run role.",
                extra={"event": "runs.ingest.no_run_role", "workspace_id": workspace_id, "upload_id": upload_id},
            )
            continue
        run_id = _run_for_workspace(workspace, upload, settings=resolved)
        if run_id is not None:
            started.append(run_id)
    _log.info(
        "Handled a VCS upload.",
        extra={"event": "runs.ingest.handled", "upload_id": upload_id, "runs": started},
    )
    return started


__all__ = [
    "CHANGED_PATHS_MEMBER",
    "INGEST_KIND",
    "MalformedIngest",
    "deterministic_id",
    "handle_record",
    "parse_body",
    "paths_match",
    "read_changed_paths",
    "trigger_patterns",
]
