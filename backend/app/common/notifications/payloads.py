"""The body each destination receives, and the generic webhook's signature.

The generic payload is HCP Terraform's notification payload, version 1, field for
field, so a receiver written for HCP works unchanged, and it is signed the same way:
`X-TFE-Notification-Signature` is the hex HMAC-SHA512 of the body under the token.
Slack gets a text fallback plus blocks and Discord one embed, each linking the run
and naming the workspace, the branch and commit, and the plan's counts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Final, Mapping

from .triggers import RUN_COMPLETED, RUN_ERRORED, RUN_NEEDS_ATTENTION, VERIFICATION

ORGANIZATION_NAME: Final = "webbpulse"
"""The organization every payload names. The plane has one tenant, which HCP calls an organization."""

PAYLOAD_VERSION: Final = 1

SIGNATURE_HEADER: Final = "X-TFE-Notification-Signature"

DISCORD_TITLE_LIMIT: Final = 256
DISCORD_DESCRIPTION_LIMIT: Final = 4096
DISCORD_FIELD_LIMIT: Final = 1024
SLACK_TEXT_LIMIT: Final = 3000

COLOURS: Final[Mapping[str, int]] = {
    RUN_COMPLETED: 0x2EB67D,
    RUN_ERRORED: 0xE01E5A,
    RUN_NEEDS_ATTENTION: 0xECB22E,
    VERIFICATION: 0x5865F2,
}
"""The Discord embed colour per trigger; anything else in progress is neutral blue."""

IN_PROGRESS_COLOUR: Final = 0x36C5F0


@dataclass(frozen=True)
class Notification:
    """Everything one delivery says, independent of where it goes.

    `run_id` is `None` for a test delivery, which carries no run, as HCP's verification does.
    """

    configuration_id: str
    configuration_name: str
    trigger: str
    title: str
    workspace_id: str
    workspace_name: str
    run_id: str | None = None
    run_url: str | None = None
    run_message: str | None = None
    run_status: str | None = None
    run_created_at: str | None = None
    run_created_by: str | None = None
    run_updated_at: str | None = None
    run_updated_by: str | None = None
    branch: str | None = None
    commit: str | None = None
    repository: str | None = None
    counts: str | None = None


def _changes(values: Any) -> tuple[int, int, int] | None:
    """The add, change and destroy counts one stored changes map holds."""
    if not isinstance(values, Mapping):
        return None
    return int(values.get("add", 0)), int(values.get("change", 0)), int(values.get("destroy", 0))


def counts_line(run: Mapping[str, Any]) -> str | None:
    """The run's resource counts as HCP words them, or `None` before a plan has finished."""
    applied = _changes(run.get("apply_changes"))
    if str(run.get("status")) == "applied" and applied is not None:
        return f"Apply: {applied[0]} added, {applied[1]} changed, {applied[2]} destroyed."
    planned = _changes(run.get("changes"))
    if planned is None:
        return None
    if planned == (0, 0, 0):
        return "Terraform plan: no changes."
    return f"Terraform plan: {planned[0]} to add, {planned[1]} to change, {planned[2]} to destroy."


def actor_name(actor: Any) -> str | None:
    """The display name of a run's recorded actor, else its id, else `None`."""
    if not isinstance(actor, Mapping):
        return None
    return str(actor.get("display_name") or actor.get("id") or "") or None


def generic_body(notification: Notification) -> dict[str, Any]:
    """HCP Terraform's notification payload, version 1."""
    return {
        "payload_version": PAYLOAD_VERSION,
        "notification_configuration_id": notification.configuration_id,
        "run_url": notification.run_url,
        "run_id": notification.run_id,
        "run_message": notification.run_message,
        "run_created_at": notification.run_created_at,
        "run_created_by": notification.run_created_by,
        "workspace_id": notification.workspace_id,
        "workspace_name": notification.workspace_name,
        "organization_name": ORGANIZATION_NAME,
        "notifications": [
            {
                "message": notification.title,
                "trigger": notification.trigger,
                "run_status": notification.run_status,
                "run_updated_at": notification.run_updated_at,
                "run_updated_by": notification.run_updated_by,
            }
        ],
    }


def _slack_escape(text: str) -> str:
    """Text safe inside Slack mrkdwn, where `&`, `<` and `>` are control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _source_line(notification: Notification) -> str | None:
    """`branch @ commit` for a VCS run, whichever halves it has."""
    parts = [part for part in (notification.branch, notification.commit) if part]
    if not parts:
        return None
    return " @ ".join(parts)


def slack_body(notification: Notification) -> dict[str, Any]:
    """A Slack incoming webhook message: a plain fallback and a section block."""
    workspace = _slack_escape(notification.workspace_name)
    headline = _slack_escape(notification.title)
    if notification.run_url and notification.run_id:
        heading = f"*{headline}* in *{workspace}*: <{notification.run_url}|{_slack_escape(notification.run_id)}>"
    else:
        heading = f"*{headline}* in *{workspace}*"
    lines = [heading]
    if notification.run_message:
        lines.append(_slack_escape(notification.run_message))
    source = _source_line(notification)
    if source:
        lines.append(f"Source: `{_slack_escape(source)}`")
    if notification.counts:
        lines.append(_slack_escape(notification.counts))
    if notification.run_created_by:
        lines.append(f"Created by {_slack_escape(notification.run_created_by)}")
    text = "\n".join(lines)[:SLACK_TEXT_LIMIT]
    return {
        "text": f"{notification.title} in {notification.workspace_name}"[:SLACK_TEXT_LIMIT],
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def _cut(text: str, limit: int) -> str:
    """`text` cut to `limit` characters, ending in an ellipsis when it was longer."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def discord_body(notification: Notification) -> dict[str, Any]:
    """A Discord webhook message with one embed and every mention disabled."""
    embed: dict[str, Any] = {
        "title": _cut(f"{notification.title}: {notification.workspace_name}", DISCORD_TITLE_LIMIT),
        "color": COLOURS.get(notification.trigger, IN_PROGRESS_COLOUR),
    }
    if notification.run_url:
        embed["url"] = notification.run_url
    if notification.run_message:
        embed["description"] = _cut(notification.run_message, DISCORD_DESCRIPTION_LIMIT)
    fields = [{"name": "Workspace", "value": notification.workspace_name, "inline": True}]
    if notification.run_id:
        fields.append({"name": "Run", "value": notification.run_id, "inline": True})
    if notification.branch:
        fields.append({"name": "Branch", "value": notification.branch, "inline": True})
    if notification.commit:
        fields.append({"name": "Commit", "value": notification.commit, "inline": True})
    if notification.counts:
        fields.append({"name": "Plan", "value": notification.counts, "inline": False})
    if notification.run_created_by:
        fields.append({"name": "Created by", "value": notification.run_created_by, "inline": True})
    embed["fields"] = [
        {"name": field["name"], "value": _cut(str(field["value"]), DISCORD_FIELD_LIMIT), "inline": field["inline"]}
        for field in fields
    ]
    if notification.run_updated_at:
        embed["timestamp"] = notification.run_updated_at
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def sign(body: bytes, token: str) -> str:
    """The hex HMAC-SHA512 of `body` under `token`, as HCP signs a generic delivery."""
    return hmac.new(token.encode("utf-8"), body, hashlib.sha512).hexdigest()


def render(destination_type: str, notification: Notification, token: str | None) -> tuple[bytes, dict[str, str]]:
    """The serialised body and the extra headers one delivery sends."""
    if destination_type == "slack":
        payload = slack_body(notification)
    elif destination_type == "discord":
        payload = discord_body(notification)
    else:
        payload = generic_body(notification)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers: dict[str, str] = {}
    if destination_type == "generic" and token:
        headers[SIGNATURE_HEADER] = sign(body, token)
    return body, headers


__all__ = [
    "ORGANIZATION_NAME",
    "PAYLOAD_VERSION",
    "SIGNATURE_HEADER",
    "Notification",
    "actor_name",
    "counts_line",
    "discord_body",
    "generic_body",
    "render",
    "sign",
    "slack_body",
]
