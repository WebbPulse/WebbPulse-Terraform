"""The tag sync consumer: importing a connected repository's existing semantic version tags.

Connecting a module and resyncing one both queue a `module_sync` message. This
consumer lists the repository's tags through the App's installation token, keeps
the `vX.Y.Z` and `X.Y.Z` ones and queues one `module_tag` message per version on
the same ingest queue, scoped to that module. Publishing is then the tag
consumer's, exactly as for a pushed tag, so a version the webhook already
published is skipped and each tarball is fetched in its own invocation.

A version already published, or already failed at the same commit, is not queued
again, since retrying cannot change a commit's bytes. At most `MAX_SYNC_VERSIONS`
of the newest versions are queued from at most `MAX_TAG_PAGES` pages of tags. A
module deleted before the message arrives syncs nothing; a fault listing tags or
queueing raises so SQS retries.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final, Mapping

from webbpulse.integrations.github import GitHubAppClient, GitHubNotConfigured

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from ....common.github.archive import list_tags
from ....common.github.loader import github_app_settings
from ....common.github.webhooks import TAG_KIND, MalformedDelivery, semver_version
from .. import service
from . import tags

_log = logging.getLogger(__name__)

KIND: Final = service.SYNC_KIND
MAX_TAG_PAGES: Final = 10
"""Pages of 100 tags read per sync, newest first by GitHub's ordering."""
MAX_SYNC_VERSIONS: Final = 100
"""The most versions one sync queues, the newest by semantic order."""
BATCH_SIZE: Final = 10


def parse_sync_message(record: Mapping[str, Any]) -> dict[str, Any]:
    """The queued sync request one SQS record carries.

    Raises:
        MalformedDelivery: The body is not a `module_sync` message naming a module.
    """
    raw = record.get("body")
    try:
        body = json.loads(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise MalformedDelivery("The body is not JSON.") from error
    if not isinstance(body, dict) or body.get("kind") != KIND:
        raise MalformedDelivery(f"The body is not a {KIND} message.")
    missing = [name for name in ("delivery", "module") if not body.get(name)]
    if missing:
        raise MalformedDelivery(f"The message lacks {', '.join(missing)}.")
    return body


def semver_tags(listed: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """Each version the tags name, against its `(tag, sha)`, `vX.Y.Z` preferred over `X.Y.Z`."""
    versions: dict[str, tuple[str, str]] = {}
    for tag, sha in listed:
        version = semver_version(tag)
        if version is None:
            continue
        if version not in versions or tag.startswith("v"):
            versions[version] = (tag, sha)
    return versions


def _settled(row: Mapping[str, Any], sha: str) -> bool:
    """Whether a stored version needs no import: published, or failed at this same commit."""
    status = row.get("status")
    return status == service.PUBLISHED or (status == service.FAILED and str(row.get("sha", "")) == sha)


def _queue(messages: list[dict[str, Any]], *, settings: Settings) -> None:
    """Send the tag messages to the ingest queue in batches, raising if any is refused."""
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
            raise RuntimeError(f"SQS refused {len(response['Failed'])} tag messages")


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> dict[str, str]:
    """Queue a publish of each semantic version tag the module has not settled, returning version against tag.

    Raises:
        MalformedDelivery: The body is not a `module_sync` message.
        TagsUnavailable: GitHub did not list the tags, so SQS retries.
    """
    resolved = settings or get_settings()
    message = parse_sync_message(record)
    extra = {"delivery": message["delivery"], "module": message["module"]}
    module = repositories.registry(resolved).get({"pk": str(message["module"]), "sk": service.MODULE_SK})
    if not module or not module.get("vcs_repo") or not module.get("vcs_installation_id"):
        _log.info("No connected module to sync.", extra={"event": "registry.sync.unconnected", **extra})
        return {}
    if not resolved.REGISTRY_INGEST_QUEUE_URL:
        _log.warning("No ingest queue to sync through.", extra={"event": "registry.sync.no_queue", **extra})
        return {}
    try:
        credentials = github_app_settings(resolved.app_secret_arn, region_name=resolved.AWS_REGION_NAME)
    except GitHubNotConfigured:
        _log.warning("No GitHub App to list tags through.", extra={"event": "registry.sync.no_app", **extra})
        return {}
    with tags.http_client() as http, GitHubAppClient.from_settings(credentials, client=http) as app:
        listed = list_tags(
            app,
            http,
            installation_id=str(module["vcs_installation_id"]),
            repository=str(module["vcs_repo"]),
            max_pages=MAX_TAG_PAGES,
        )
    found = semver_tags(listed)
    existing = {
        str(row["version"]): row
        for row in service.version_rows(
            str(module["namespace"]), str(module["name"]), str(module["provider"]), settings=resolved
        )
    }
    wanted = sorted(found, key=service.version_order, reverse=True)[:MAX_SYNC_VERSIONS]
    queued: dict[str, str] = {}
    messages: list[dict[str, Any]] = []
    for version in wanted:
        tag, sha = found[version]
        if version in existing and _settled(existing[version], sha):
            continue
        queued[version] = tag
        messages.append(
            {
                "kind": TAG_KIND,
                "delivery": str(message["delivery"]),
                "event": "sync",
                "repo": str(module["vcs_repo"]),
                "repository_id": str(module["vcs_repository_id"]),
                "installation_id": str(module["vcs_installation_id"]),
                "actor": str(message.get("actor") or "sync"),
                "ref": f"refs/tags/{tag}",
                "tag": tag,
                "version": version,
                "sha": sha,
                "module": str(message["module"]),
            }
        )
    _queue(messages, settings=resolved)
    _log.info(
        "Synced a module's tags.",
        extra={
            "event": "registry.sync.queued",
            "tags_listed": len(listed),
            "versions_found": len(found),
            "versions_queued": len(messages),
            **extra,
        },
    )
    return queued


__all__ = [
    "BATCH_SIZE",
    "KIND",
    "MAX_SYNC_VERSIONS",
    "MAX_TAG_PAGES",
    "handle_record",
    "parse_sync_message",
    "semver_tags",
]
