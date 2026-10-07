"""Turning a verified GitHub App delivery into an ingest upload.

The github function checked the signature and queued a compact message. This
consumer does what the Actions workflow used to do on a runner: it resolves the
commit, works out the changed paths, fetches the repository at that commit through
the App's installation token and writes the same ingest record and the same
`ingest/<upload id>.tar.gz` object. From there the existing ingest consumer starts
the runs and the existing reporting posts the checks, so nothing downstream can
tell a webhook upload from a workflow one.

A pull request only plans on the workspaces whose tracked branch, or the
repository's default branch when none is set, is the pull request's base branch,
as HCP Terraform does, so a pull request into `staging` never plans a workspace
tracking `main`.

A delivery no bound workspace would run on never fetches anything. A push to a
branch that some bound workspace tracks, and every pull request, has its record
written and is reported straight away as "No runs needed". A push to a branch no
bound workspace tracks posts no check at all, as HCP Terraform does, since the pull
request delivery owns the aggregate on a pull request's head commit.

A pull request is planned against GitHub's merge commit, exactly as the workflow's
`refs/pull/<n>/merge` checkout was. GitHub computes that commit after the event, so
the pull request is read until its merge state settles; a request whose head has
moved on is left to the newer delivery, and one that cannot merge is dropped.

The upload id is derived from the delivery id, so a redelivered message lands on
the same record and the same key, and one already written is not written again.
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed
from webbpulse.integrations.github import PullRequest

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from ....common.github.archive import TarballUnavailable, download_tarball
from ....common.github.webhooks import PUSH, WEBHOOK_KIND, parse_message
from ....common.workspaces import vcs as workspace_vcs
from .. import reporting
from ..vcs import INGEST_CONTENT_TYPE, RECORD_TTL, UPLOAD_ID_PREFIX, crockford, ingest_key
from . import ingest

_log = logging.getLogger(__name__)

KIND: Final = WEBHOOK_KIND

MAX_TARBALL_BYTES: Final = 200_000_000
"""The largest repository archive fetched, so the download and the repack both fit
in the function's temporary storage."""

MERGEABLE_ATTEMPTS: Final = 4
MERGEABLE_WAIT_SECONDS: Final = 2.0

EXCLUDED_PARTS: Final = frozenset({".git", ".terraform"})

sleep: Callable[[float], None] = time.sleep
"""How the merge state wait pauses. The seam the tests replace."""


class MergeStatePending(Exception):
    """GitHub has not computed the pull request's merge commit yet, so the message is retried."""


def upload_id_for(delivery: str) -> str:
    """The upload id one delivery gets, the same on every redelivery of the message."""
    return UPLOAD_ID_PREFIX + crockford(hashlib.sha256(f"webhook:{delivery}".encode()).digest())


def _record(upload_id: str, message: Mapping[str, Any], resolved: Mapping[str, Any]) -> dict[str, Any]:
    """The ingest record one delivery writes, shaped like the workflow's."""
    now = datetime.now(timezone.utc)
    received = int(message.get("received_at_ms") or now.timestamp() * 1000)
    item: dict[str, Any] = {
        "upload_id": upload_id,
        "key": ingest_key(upload_id),
        "source": "webhook",
        "delivery": str(message["delivery"]),
        "repo": str(message["repo"]),
        "repository_id": str(message["repository_id"]),
        "repository_owner": str(message["repo"]).split("/", 1)[0],
        "event": str(message["event"]),
        "ref": str(message["ref"]),
        "sha": str(resolved["sha"]),
        "actor": str(message["actor"]),
        "installation_id": int(message["installation_id"]),
        "size_bytes": 0,
        "created_at": datetime.fromtimestamp(received / 1000, timezone.utc).isoformat(),
        "created_at_ms": received,
        "expires_at": int((now + RECORD_TTL).timestamp()),
    }
    if message["event"] == PUSH:
        item["branch"] = str(message["branch"])
    else:
        item["pr_number"] = int(message["pr_number"])
        item["head_sha"] = str(resolved["head_sha"])
        for field in ("base_sha", "base_branch", "default_branch"):
            if message.get(field):
                item[field] = str(message[field])
    return item


def _settled_pull(reader: reporting.GitHubReader, message: Mapping[str, Any]) -> Optional[PullRequest]:
    """The pull request once its merge state has settled, or `None` when this delivery is moot.

    Raises:
        MergeStatePending: GitHub is still computing the merge commit.
    """
    number = int(message["pr_number"])
    for attempt in range(MERGEABLE_ATTEMPTS):
        pull = reader.client.get_pull_request(reader.repository, number, installation_id=reader.installation)
        if pull.head_sha != str(message["head_sha"]):
            return None
        if (pull.state or "open") != "open" or pull.mergeable is False:
            return None
        if pull.mergeable is True and pull.merge_commit_sha:
            return pull
        if attempt + 1 < MERGEABLE_ATTEMPTS:
            sleep(MERGEABLE_WAIT_SECONDS)
    raise MergeStatePending(f"pull request {number} has no merge commit yet")


def _pull_paths(reader: reporting.GitHubReader, number: int) -> Optional[list[str]]:
    """The paths a pull request changes, or `None` for every path once the listing is capped."""
    files = reader.client.list_pull_request_files(
        reader.repository, number, max_pages=reporting.MAX_PAGES, installation_id=reader.installation
    )
    if len(files) >= reporting.PAGE_SIZE * reporting.MAX_PAGES:
        return None
    paths: set[str] = set()
    for item in files:
        paths.add(item.filename)
        if item.previous_filename:
            paths.add(item.previous_filename)
    return sorted(path for path in paths if path)


def resolve(reader: reporting.GitHubReader, message: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The commit to plan, the head it reports on and the changed paths, or `None` to drop the delivery."""
    if message["event"] == PUSH:
        return {"sha": str(message["sha"]), "head_sha": None, "paths": message.get("paths")}
    pull = _settled_pull(reader, message)
    if pull is None:
        return None
    return {
        "sha": str(pull.merge_commit_sha),
        "head_sha": str(message["head_sha"]),
        "paths": _pull_paths(reader, int(message["pr_number"])),
    }


def _matched(workspaces: Iterable[Mapping[str, Any]], upload: Mapping[str, Any], paths: Optional[list[str]]) -> bool:
    """Whether any bound workspace would run on this upload, by the ingest consumer's own rules."""
    return any(
        ingest.always_triggers(workspace) or ingest.paths_match(ingest.trigger_patterns(workspace), paths)
        for workspace in workspaces
        if ingest._eligible(workspace, upload)  # pyright: ignore[reportPrivateUsage]
    )


def _tracked(workspaces: Iterable[Mapping[str, Any]], branch: str) -> bool:
    """Whether any bound workspace tracks `branch`."""
    return any(str(workspace.get("tracked_branch") or "") == branch for workspace in workspaces if branch)


def _relative(name: str) -> Optional[str]:
    """The member's path under GitHub's top level directory, or `None` to leave it out."""
    parts = [part for part in name.split("/")[1:] if part]
    if not parts or any(part in ("..", ".") for part in parts):
        return None
    if parts[0] == ".webbpulse" or any(part in EXCLUDED_PARTS for part in parts):
        return None
    if parts[-1].endswith(".tfstate") or ".tfstate." in parts[-1]:
        return None
    return "/".join(parts)


def _member(name: str, *, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    """A normalized archive entry, owner and time stripped the way the workflow built them."""
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = 0o755 if directory else 0o644
    info.size = 0 if directory else size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def repack(source: Path, target: Path, paths: Optional[list[str]]) -> None:
    """Rewrite GitHub's archive into the layout the runner unpacks.

    The top level `owner-repo-sha/` directory is stripped so the configuration sits
    at the root, links are left out since the runner refuses them, and
    `.webbpulse/changed-paths.txt` is added, `*` meaning every path.
    """
    listing = ("*\n" if paths is None else "".join(f"{path}\n" for path in paths)).encode()
    with tarfile.open(source, "r|gz") as incoming, tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as outgoing:
        outgoing.addfile(_member(".webbpulse", directory=True))
        outgoing.addfile(_member(ingest.CHANGED_PATHS_MEMBER, size=len(listing)), io.BytesIO(listing))
        for member in incoming:
            name = _relative(member.name)
            if name is None:
                continue
            if member.isdir():
                outgoing.addfile(_member(name, directory=True))
            elif member.isfile():
                handle = incoming.extractfile(member)
                if handle is not None:
                    info = _member(name, size=member.size)
                    info.mode = 0o755 if member.mode & 0o111 else 0o644
                    outgoing.addfile(info, handle)


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def _object_exists(key: str, settings: Settings) -> bool:
    """Whether the ingest object is already in the artifacts bucket."""
    from botocore.exceptions import ClientError

    try:
        _s3(settings).head_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key)
    except ClientError:
        return False
    return True


def _put_record(item: Mapping[str, Any], settings: Settings) -> bool:
    """Write the ingest record once. Returns False when an earlier delivery already did."""
    try:
        repositories.vcs_uploads(settings).put(dict(item), condition=Attr("upload_id").not_exists())
    except ConditionFailed:
        return False
    return True


def _ingest(
    reader: reporting.GitHubReader, item: dict[str, Any], paths: Optional[list[str]], settings: Settings
) -> None:
    """Fetch, repack and upload the configuration, then write the record the ingest consumer reads.

    The record goes in before the object, since the object's arrival is what starts
    the ingest consumer and it reads the record by the upload id in the key.
    """
    with tempfile.TemporaryDirectory() as scratch:
        downloaded = Path(scratch) / "github.tar.gz"
        packed = Path(scratch) / "configuration.tar.gz"
        download_tarball(
            reader.client,
            installation_id=reader.installation,
            repository=reader.repository,
            ref=str(item["sha"]),
            target=downloaded,
            max_bytes=MAX_TARBALL_BYTES,
        )
        repack(downloaded, packed, paths)
        downloaded.unlink()
        item["size_bytes"] = packed.stat().st_size
        uploads = repositories.vcs_uploads(settings)
        if not _put_record(item, settings):
            uploads.update(
                {"upload_id": item["upload_id"]},
                update_expression="SET size_bytes = :size",
                expression_values={":size": item["size_bytes"]},
            )
        _s3(settings).upload_file(
            str(packed),
            settings.ARTIFACTS_BUCKET,
            str(item["key"]),
            ExtraArgs={"ContentType": INGEST_CONTENT_TYPE},
        )


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> Optional[str]:
    """Ingest one queued delivery. Returns the upload id, or `None` when it was dropped.

    Raises:
        MalformedDelivery: The body is not a usable `github_webhook` message.
        MergeStatePending: A pull request's merge commit is not ready, so SQS retries.
        TarballUnavailable: The archive could not be fetched, so SQS retries.
        GitHubError: GitHub failed a read, so SQS retries.
    """
    resolved_settings = settings or get_settings()
    message = parse_message(record)
    upload_id = upload_id_for(str(message["delivery"]))
    extra: dict[str, Any] = {"upload_id": upload_id, "delivery": message["delivery"], "repository": message["repo"]}
    existing = repositories.vcs_uploads(resolved_settings).get({"upload_id": upload_id}, consistent=True)
    if existing is not None and (
        existing.get("report_only") or _object_exists(ingest_key(upload_id), resolved_settings)
    ):
        _log.info("Skipped a delivery already ingested.", extra={"event": "runs.webhook.duplicate", **extra})
        return upload_id
    with reporting.app_reader(str(message["repo"]), resolved_settings) as reader:
        if reader is None:
            _log.warning("No GitHub App to fetch a delivery through.", extra={"event": "runs.webhook.no_app", **extra})
            return None
        resolved = resolve(reader, message)
        if resolved is None:
            _log.info("Dropped a delivery GitHub has moved past.", extra={"event": "runs.webhook.moot", **extra})
            return None
        item = _record(upload_id, message, resolved)
        bound = workspace_vcs.bound_workspaces(
            str(message["repo"]), str(message["repository_id"]), settings=resolved_settings
        )
        for workspace in bound:
            if workspace.get("vcs_repository_id") is None:
                workspace_vcs.record_repository_id(
                    str(workspace["workspace_id"]), str(message["repository_id"]), settings=resolved_settings
                )
        if message["event"] == PUSH and not _tracked(bound, str(message["branch"])):
            _log.info(
                "Dropped a push to a branch no workspace tracks.", extra={"event": "runs.webhook.untracked", **extra}
            )
            return None
        if not _matched(bound, item, resolved["paths"]):
            item["report_only"] = True
            if _put_record(item, resolved_settings):
                reporting.report_upload(item, [], settings=resolved_settings)
            _log.info("No bound workspace runs on a delivery.", extra={"event": "runs.webhook.no_match", **extra})
            return upload_id
        _ingest(reader, item, resolved["paths"], resolved_settings)
    _log.info(
        "Ingested a delivery.",
        extra={"event": "runs.webhook.ingested", "vcs_event": message["event"], "size": item["size_bytes"], **extra},
    )
    return upload_id


__all__ = [
    "KIND",
    "MAX_TARBALL_BYTES",
    "MergeStatePending",
    "TarballUnavailable",
    "handle_record",
    "repack",
    "resolve",
    "upload_id_for",
]
