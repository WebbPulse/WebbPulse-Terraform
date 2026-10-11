"""Email the owner when something sensitive happens on the production plane.

Two sources feed this one function, and every alert leaves through the alarm SNS
topic, which emails the owner:

- The audit table's stream. The event source mapping only delivers inserts of the
  actions worth an alert; this narrows a few of them further, such as a workspace
  update that moved the run role, or a device grant that carries `runs:apply`.
- The access gate login Lambda's log group, through a subscription filter on its
  sign-in line. A sign-in alerts when its address or its user agent is new for that
  email, judged against a small sightings table that forgets after a year.

An alert names the event, the actor, the workspace or other target, and the time.
It never carries a variable's value, a key, a token or plan output; the audit
payload is read only for scopes, which are names.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Iterable, Mapping

import boto3
from boto3.dynamodb.types import TypeDeserializer

_log = logging.getLogger()
_log.setLevel(logging.INFO)

SIGHTING_TTL_SECONDS = 365 * 24 * 3600
"""How long a sign-in address or device is remembered before it counts as new again."""

ROLE_FIELDS = frozenset({"run_role_arn", "pending_run_role_arn"})
"""Workspace fields whose change is a credentials change, so a general update alerts on them."""

APPLY_SCOPE = "runs:apply"

LABELS = {
    "api_key.created": "API key created",
    "device_grant.opened": "CLI device login with runs:apply",
    "run.confirmed": "Run confirmed for apply",
    "variable.written": "Workspace variable written",
    "variable.deleted": "Workspace variable deleted",
    "workspace.updated": "Workspace run role changed",
    "workspace.plan_access_changed": "Workspace plan access changed",
    "workspace.auto_apply_changed": "Workspace auto-apply changed",
    "workspace.run_api_scopes_changed": "Workspace run API token scopes changed",
    "workspace.aws_connected": "Workspace connected to AWS",
    "github.app_created": "GitHub App created",
    "github.webhook_synced": "GitHub App webhook synced",
    "github.installation_recorded": "GitHub App installation recorded",
    "github.installation_removed": "GitHub App installation removed",
}
"""Every action that alerts, and the subject line it gets."""

LEGACY_SIGN_IN = re.compile(r"session issued \{ email: '([^']*)'")
"""The gate's sign-in line before it was JSON, which carries no address or user agent."""

_deserializer = TypeDeserializer()
_dynamodb = None
_sns = None


def _ddb() -> Any:
    """The DynamoDB resource, built once per container."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _topic() -> Any:
    """The SNS client, built once per container."""
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _image(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A stream record's image as plain Python values."""
    return {key: _deserializer.deserialize(value) for key, value in raw.items()}


def _strings(value: Any) -> list[str]:
    """A list or set of names as strings, empty for anything else."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(item) for item in value)
    return []


def _field_names(value: Any) -> set[str]:
    """The keys of a stored before or after map."""
    return {str(key) for key in value} if isinstance(value, Mapping) else set()


def wants(item: Mapping[str, Any]) -> bool:
    """Whether an audit event is one the owner is emailed about."""
    action = str(item.get("action", ""))
    if action not in LABELS:
        return False
    if action == "workspace.updated":
        return bool(ROLE_FIELDS & (_field_names(item.get("after")) | _field_names(item.get("before"))))
    if action == "device_grant.opened":
        payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
        return APPLY_SCOPE in _strings(payload.get("scopes"))
    return True


def _user_email(user_id: str) -> str:
    """The person's email from the users table, or empty when unknown."""
    if not user_id or not os.environ.get("USERS_TABLE"):
        return ""
    try:
        item = _ddb().Table(os.environ["USERS_TABLE"]).get_item(Key={"id": user_id}).get("Item") or {}
    except Exception:
        _log.exception("users_lookup_failed")
        return ""
    return str(item.get("email", "") or "")


def _workspace_name(workspace_id: str) -> str:
    """The workspace's name from the workspaces table, or empty when unknown."""
    if not workspace_id or not os.environ.get("WORKSPACES_TABLE"):
        return ""
    try:
        item = (
            _ddb().Table(os.environ["WORKSPACES_TABLE"]).get_item(Key={"workspace_id": workspace_id}).get("Item") or {}
        )
    except Exception:
        _log.exception("workspace_lookup_failed")
        return ""
    return str(item.get("name", "") or "")


def _target_line(item: Mapping[str, Any]) -> str:
    """The target as the email names it: a workspace by name, anything else by type and label."""
    target_type = str(item.get("target_type", "") or "")
    target_id = str(item.get("target_id", "") or "")
    label = str(item.get("target_label", "") or "")
    if target_type == "workspace":
        name = label or _workspace_name(target_id)
        return f"Workspace: {name} ({target_id})" if name else f"Workspace: {target_id}"
    if target_type == "api_key":
        return f"API key: {label or 'unnamed'}"
    if target_type == "device_grant":
        return f"Device grant client: {label or 'unknown'}"
    if target_type == "github_app":
        return f"GitHub App: {label or target_id}"
    return f"Target: {target_type}"


def audit_message(item: Mapping[str, Any]) -> tuple[str, str]:
    """The subject and body for one audit event, naming who, what, where and when."""
    action = str(item.get("action", ""))
    actor_id = str(item.get("actor_id", "") or "")
    kind = str(item.get("actor_kind", "") or "")
    email = _user_email(actor_id) if kind in {"user", "api_key"} else ""
    who = f"{email} ({kind})" if email else f"{actor_id} ({kind})"
    lines = [
        f"Event: {LABELS[action]} [{action}]",
        f"Actor: {who}",
        f"Source: {item.get('source') or 'unknown'}, address {item.get('ip') or 'unknown'}",
        _target_line(item),
        f"Time: {item.get('occurred_at', '')}",
    ]
    payload = item.get("payload") if isinstance(item.get("payload"), Mapping) else {}
    scopes = _strings(payload.get("scopes"))
    if scopes and action in {"api_key.created", "device_grant.opened"}:
        lines.append(f"Scopes: {', '.join(scopes)}")
    lines.append(f"Audit event: {item.get('event_id', '')}")
    site = os.environ.get("SITE_URL", "")
    if site:
        lines.append(f"Audit log: {site}/settings/audit")
    return LABELS[action], "\n".join(lines)


def _publish(subject: str, body: str) -> None:
    """Send one alert through the alarm topic."""
    prefix = os.environ.get("SUBJECT_PREFIX", "WebbPulse Terraform")
    _topic().publish(
        TopicArn=os.environ["TOPIC_ARN"],
        Subject=f"[{prefix}] {subject}"[:100],
        Message=body + "\n",
    )


def _audit_records(records: Iterable[Mapping[str, Any]]) -> int:
    """Alert on each wanted audit insert, answering how many were sent."""
    sent = 0
    for record in records:
        if record.get("eventName") != "INSERT":
            continue
        image = _image(record.get("dynamodb", {}).get("NewImage", {}))
        if not wants(image):
            continue
        subject, body = audit_message(image)
        _publish(subject, body)
        _log.info("security_alert_sent", extra={"action": image.get("action"), "event_id": image.get("event_id")})
        sent += 1
    return sent


def parse_sign_in(message: str) -> dict[str, str] | None:
    """One gate log line as a sign-in, from its JSON form or the older text form, else `None`."""
    start = message.find("{")
    if start >= 0 and "gate_sign_in" in message:
        try:
            data = json.loads(message[start:])
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("event") == "gate_sign_in":
            return {
                "email": str(data.get("email", "") or "").lower(),
                "host": str(data.get("host", "") or ""),
                "ip": str(data.get("ip", "") or "unknown"),
                "user_agent": str(data.get("user_agent", "") or ""),
            }
    legacy = LEGACY_SIGN_IN.search(message)
    if legacy:
        return {"email": legacy.group(1).lower(), "host": "", "ip": "unknown", "user_agent": ""}
    return None


def _first_sighting(email: str, marker: str, now: int) -> bool:
    """Record that `email` signed in with `marker`, answering whether it had not been seen before."""
    table = _ddb().Table(os.environ["SIGHTINGS_TABLE"])
    result = table.update_item(
        Key={"email": email, "marker": marker},
        UpdateExpression="SET first_seen = if_not_exists(first_seen, :now), last_seen = :now, expires_at = :ttl",
        ExpressionAttributeValues={":now": now, ":ttl": now + SIGHTING_TTL_SECONDS},
        ReturnValues="UPDATED_OLD",
    )
    return "first_seen" not in result.get("Attributes", {})


def _device(user_agent: str) -> str:
    """A short stable fingerprint of a user agent, so the table never stores the string."""
    return hashlib.sha256(user_agent.encode()).hexdigest()[:16]


def _sign_in_records(log_events: Iterable[Mapping[str, Any]]) -> int:
    """Alert on each gate sign-in from a new address or device, answering how many were sent."""
    sent = 0
    for log_event in log_events:
        sign_in = parse_sign_in(str(log_event.get("message", "")))
        if sign_in is None or not sign_in["email"]:
            continue
        now = int(time.time())
        new = []
        if sign_in["ip"] != "unknown" and _first_sighting(sign_in["email"], f"ip#{sign_in['ip']}", now):
            new.append("address")
        if sign_in["user_agent"] and _first_sighting(sign_in["email"], f"ua#{_device(sign_in['user_agent'])}", now):
            new.append("device")
        if not new:
            continue
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(log_event.get("timestamp", now * 1000)) / 1000))
        body = "\n".join(
            [
                f"Event: Access gate sign-in from a new {' and '.join(new)} [gate.sign_in]",
                f"Actor: {sign_in['email']}",
                f"Address: {sign_in['ip']}",
                f"User agent: {sign_in['user_agent'] or 'unknown'}",
                f"Site: {sign_in['host'] or 'unknown'}",
                f"Time: {stamp}",
            ]
        )
        _publish(f"Gate sign-in from a new {' and '.join(new)}", body)
        _log.info("security_alert_sent", extra={"action": "gate.sign_in"})
        sent += 1
    return sent


def handler(event: Mapping[str, Any], _context: Any = None) -> dict[str, int]:
    """Route a stream batch or a log subscription delivery to its alerts."""
    if "awslogs" in event:
        payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
        return {"sent": _sign_in_records(payload.get("logEvents", []))}
    return {"sent": _audit_records(event.get("Records", []))}
