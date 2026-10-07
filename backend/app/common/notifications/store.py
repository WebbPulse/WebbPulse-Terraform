"""Notification configurations: validation, sealed storage and the rendered view.

A webhook URL is a credential, since anyone holding a Slack or Discord webhook URL
can post to the channel, and a generic webhook's token signs every delivery. Both
are sealed with the sensitive variables' cipher under their own purpose, bound to
one workspace and one configuration, and neither is ever rendered: a reader sees
the scheme and host only, and whether a token is set.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Final, Literal, Mapping
from urllib.parse import urlsplit

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso
from webbpulse.identity.crypto import EnvelopeDecryptionFailed, SealedSecret

from ..composition.settings import Settings, get_settings
from ..core import variable_cipher
from ..db import repositories
from .triggers import ALL_TRIGGERS

DestinationType = Literal["slack", "discord", "generic"]
"""Where a configuration delivers. HCP's email and Microsoft Teams destinations are not offered."""

DESTINATION_TYPES: Final = ("slack", "discord", "generic")

NOTIFICATION_ID_PREFIX: Final = "nc-"

URL_PURPOSE: Final = "notification-webhook-url"
"""The encryption context purpose a webhook URL is sealed under, so it never opens as a variable."""

TOKEN_PURPOSE: Final = "notification-webhook-token"
"""The purpose a generic webhook's signing token is sealed under."""

URL_ATTRIBUTE: Final = "url_sealed"
TOKEN_ATTRIBUTE: Final = "token_sealed"

MAX_CONFIGURATIONS_PER_WORKSPACE: Final = 50
"""A generous abuse limit. Every run transition fans out one queued delivery per
matching configuration, so the ceiling is the stream batch's time budget, far above this."""

MAX_URL_LENGTH: Final = 2048
MAX_TOKEN_LENGTH: Final = 256
MAX_NAME_LENGTH: Final = 128

SLACK_PREFIXES: Final = ("https://hooks.slack.com/",)
DISCORD_PREFIXES: Final = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)

LAST_DELIVERY_ATTRIBUTE: Final = "last_delivery"


class NotificationNotFound(Exception):
    """No configuration with this id on this workspace."""


class TooManyConfigurations(Exception):
    """The workspace already holds `MAX_CONFIGURATIONS_PER_WORKSPACE` configurations."""


class InvalidConfiguration(ValueError):
    """A configuration the API refuses, with a message safe to return: it never quotes the URL."""


def _repository(settings: Settings):
    """The configurations table."""
    return repositories.notification_configurations(settings)


def _subject(workspace_id: str, notification_id: str) -> str:
    """The encryption context subject: the one configuration a sealed value belongs to."""
    return f"{workspace_id}#{notification_id}"


def _seal(value: str, *, purpose: str, workspace_id: str, notification_id: str, settings: Settings) -> dict[str, str]:
    """Seal one secret into the map its attribute stores."""
    sealed = variable_cipher.cipher(settings).seal(
        value.encode("utf-8"), user_id=_subject(workspace_id, notification_id), purpose=purpose
    )
    return sealed.as_item()


def _open(stored: Any, *, purpose: str, workspace_id: str, notification_id: str, settings: Settings) -> str:
    """Open one sealed secret, raising `EnvelopeDecryptionFailed` when it does not authenticate."""
    sealed = SealedSecret.from_item(stored) if isinstance(stored, Mapping) else None
    if sealed is None:
        raise EnvelopeDecryptionFailed("the configuration carries no usable ciphertext")
    plaintext = variable_cipher.cipher(settings).open(
        sealed, user_id=_subject(workspace_id, notification_id), purpose=purpose
    )
    return plaintext.decode("utf-8")


def mask_url(url: str) -> str:
    """The scheme and host of a webhook URL with its path hidden, the only form ever shown."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}/****"


def validate_url(destination_type: str, url: str) -> str:
    """The URL trimmed, or `InvalidConfiguration` naming what is wrong without quoting it.

    Every destination needs HTTPS. Slack and Discord URLs must be their own incoming
    webhook endpoints, which also keeps those destinations from being pointed anywhere
    else. A host that is a literal private address is refused here; a name resolving to
    one is refused at delivery.
    """
    trimmed = url.strip()
    if not trimmed or len(trimmed) > MAX_URL_LENGTH:
        raise InvalidConfiguration(f"The URL must be 1 to {MAX_URL_LENGTH} characters.")
    try:
        parts = urlsplit(trimmed)
        port = parts.port
    except ValueError as error:
        raise InvalidConfiguration("The URL is not a valid URL.") from error
    if parts.scheme != "https" or not parts.hostname:
        raise InvalidConfiguration("The URL must be an https:// URL with a host.")
    if parts.username is not None or parts.password is not None:
        raise InvalidConfiguration("The URL must not carry a user name or password.")
    if any(character.isspace() for character in trimmed):
        raise InvalidConfiguration("The URL must not contain spaces.")
    if destination_type == "slack" and not trimmed.startswith(SLACK_PREFIXES):
        raise InvalidConfiguration("A Slack URL must be an incoming webhook URL on https://hooks.slack.com/.")
    if destination_type == "discord" and not trimmed.startswith(DISCORD_PREFIXES):
        raise InvalidConfiguration("A Discord URL must be a channel webhook URL on https://discord.com/api/webhooks/.")
    if port is not None and not 1 <= port <= 65535:
        raise InvalidConfiguration("The URL's port is out of range.")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise InvalidConfiguration("The URL must not point at a private, loopback or link-local address.")
    if parts.hostname.lower() in {"localhost", "localhost.localdomain"} or parts.hostname.lower().endswith(
        (".localhost", ".internal", ".local")
    ):
        raise InvalidConfiguration("The URL must point at a public host.")
    return trimmed


def validate_triggers(triggers: Any) -> list[str]:
    """The triggers deduplicated in run order, or `InvalidConfiguration` for an unknown one."""
    requested = list(triggers or [])
    unknown = sorted({str(trigger) for trigger in requested} - set(ALL_TRIGGERS))
    if unknown:
        raise InvalidConfiguration(f"Unknown trigger(s): {', '.join(unknown)}. Use any of {', '.join(ALL_TRIGGERS)}.")
    chosen = set(requested)
    return [trigger for trigger in ALL_TRIGGERS if trigger in chosen]


def _validate_token(destination_type: str, token: str | None) -> str | None:
    """A generic webhook's signing token, or `None`. Slack and Discord sign nothing."""
    if token is None or token == "":
        return None
    if destination_type != "generic":
        raise InvalidConfiguration("Only a generic webhook takes a token.")
    if len(token) > MAX_TOKEN_LENGTH:
        raise InvalidConfiguration(f"The token must be at most {MAX_TOKEN_LENGTH} characters.")
    return token


def _validate_name(name: Any) -> str:
    """A trimmed display name."""
    trimmed = str(name or "").strip()
    if not trimmed or len(trimmed) > MAX_NAME_LENGTH:
        raise InvalidConfiguration(f"The name must be 1 to {MAX_NAME_LENGTH} characters.")
    return trimmed


def list_configurations(workspace_id: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every configuration on one workspace, oldest first."""
    resolved = settings or get_settings()
    return list(_repository(resolved).iter_query(Key("workspace_id").eq(workspace_id)))


def get_configuration(workspace_id: str, notification_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One configuration, or `NotificationNotFound`."""
    resolved = settings or get_settings()
    item = _repository(resolved).get({"workspace_id": workspace_id, "notification_id": notification_id})
    if item is None:
        raise NotificationNotFound(notification_id)
    return item


def create_configuration(
    workspace_id: str, payload: Mapping[str, Any], *, settings: Settings | None = None
) -> dict[str, Any]:
    """Store a new configuration with its URL and token sealed.

    Raises:
        InvalidConfiguration: A field the API refuses.
        TooManyConfigurations: The workspace is at its limit.
        MasterKeyUnavailable: No key to seal the URL with; nothing is stored in the clear.
    """
    resolved = settings or get_settings()
    destination_type = str(payload.get("destination_type", ""))
    if destination_type not in DESTINATION_TYPES:
        raise InvalidConfiguration(f"destination_type must be one of {', '.join(DESTINATION_TYPES)}.")
    name = _validate_name(payload.get("name"))
    url = validate_url(destination_type, str(payload.get("url") or ""))
    token = _validate_token(destination_type, payload.get("token"))
    triggers = validate_triggers(payload.get("triggers"))
    if len(list_configurations(workspace_id, settings=resolved)) >= MAX_CONFIGURATIONS_PER_WORKSPACE:
        raise TooManyConfigurations(workspace_id)
    notification_id = f"{NOTIFICATION_ID_PREFIX}{new_ulid()}"
    timestamp = now_iso()
    item: dict[str, Any] = {
        "workspace_id": workspace_id,
        "notification_id": notification_id,
        "name": name,
        "destination_type": destination_type,
        "enabled": bool(payload.get("enabled", True)),
        "triggers": triggers,
        "url_masked": mask_url(url),
        URL_ATTRIBUTE: _seal(
            url, purpose=URL_PURPOSE, workspace_id=workspace_id, notification_id=notification_id, settings=resolved
        ),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if token is not None:
        item[TOKEN_ATTRIBUTE] = _seal(
            token, purpose=TOKEN_PURPOSE, workspace_id=workspace_id, notification_id=notification_id, settings=resolved
        )
    _repository(resolved).put(item, condition=Attr("notification_id").not_exists())
    return item


def update_configuration(
    workspace_id: str,
    notification_id: str,
    changes: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Apply a partial update. A field absent from `changes` is left alone.

    A new `url` is validated against the stored destination and resealed. A `token`
    of `None` or an empty string clears it. Changing the destination needs a new URL,
    since the stored one was only checked against the old destination.
    """
    resolved = settings or get_settings()
    existing = get_configuration(workspace_id, notification_id, settings=resolved)
    destination_type = str(changes.get("destination_type") or existing["destination_type"])
    if destination_type not in DESTINATION_TYPES:
        raise InvalidConfiguration(f"destination_type must be one of {', '.join(DESTINATION_TYPES)}.")
    if destination_type != existing["destination_type"] and not changes.get("url"):
        raise InvalidConfiguration("Changing the destination type needs a new URL.")
    attributes: dict[str, Any] = {"destination_type": destination_type, "updated_at": now_iso()}
    removals: list[str] = []
    if "name" in changes:
        attributes["name"] = _validate_name(changes["name"])
    if "enabled" in changes and changes["enabled"] is not None:
        attributes["enabled"] = bool(changes["enabled"])
    if "triggers" in changes and changes["triggers"] is not None:
        attributes["triggers"] = validate_triggers(changes["triggers"])
    if changes.get("url"):
        url = validate_url(destination_type, str(changes["url"]))
        attributes["url_masked"] = mask_url(url)
        attributes[URL_ATTRIBUTE] = _seal(
            url, purpose=URL_PURPOSE, workspace_id=workspace_id, notification_id=notification_id, settings=resolved
        )
    if "token" in changes:
        token = _validate_token(destination_type, changes["token"])
        if token is None:
            removals.append(TOKEN_ATTRIBUTE)
        else:
            attributes[TOKEN_ATTRIBUTE] = _seal(
                token,
                purpose=TOKEN_PURPOSE,
                workspace_id=workspace_id,
                notification_id=notification_id,
                settings=resolved,
            )
    elif destination_type != "generic" and TOKEN_ATTRIBUTE in existing:
        removals.append(TOKEN_ATTRIBUTE)
    names = {f"#a{index}": key for index, key in enumerate(attributes)}
    values = {f":v{index}": value for index, value in enumerate(attributes.values())}
    expression = "SET " + ", ".join(f"#a{index} = :v{index}" for index in range(len(attributes)))
    if removals:
        removal_names = {f"#r{index}": key for index, key in enumerate(removals)}
        names.update(removal_names)
        expression += " REMOVE " + ", ".join(removal_names)
    try:
        updated = _repository(resolved).update(
            {"workspace_id": workspace_id, "notification_id": notification_id},
            update_expression=expression,
            expression_values=values,
            expression_names=names,
            condition=Attr("notification_id").exists(),
            return_values="ALL_NEW",
        )
    except ConditionFailed as error:
        raise NotificationNotFound(notification_id) from error
    return dict(updated or {})


def delete_configuration(workspace_id: str, notification_id: str, *, settings: Settings | None = None) -> None:
    """Delete one configuration, or `NotificationNotFound`."""
    resolved = settings or get_settings()
    try:
        _repository(resolved).delete(
            {"workspace_id": workspace_id, "notification_id": notification_id},
            condition=Attr("notification_id").exists(),
        )
    except ConditionFailed as error:
        raise NotificationNotFound(notification_id) from error


def delete_workspace_configurations(workspace_id: str, *, settings: Settings | None = None) -> None:
    """Delete every configuration on a workspace being deleted."""
    resolved = settings or get_settings()
    keys = [
        {"workspace_id": workspace_id, "notification_id": str(item["notification_id"])}
        for item in list_configurations(workspace_id, settings=resolved)
    ]
    if keys:
        _repository(resolved).delete_many(keys)


def matching_configurations(
    workspace_id: str, trigger: str, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """The enabled configurations on a workspace subscribed to one trigger."""
    return [
        item
        for item in list_configurations(workspace_id, settings=settings)
        if item.get("enabled", True) and trigger in (item.get("triggers") or [])
    ]


def secrets(item: Mapping[str, Any], *, settings: Settings | None = None) -> tuple[str, str | None]:
    """The opened URL and token of one configuration, for a delivery only.

    Raises:
        EnvelopeDecryptionFailed: The sealed values do not authenticate against this row.
        MasterKeyUnavailable: No key is configured.
    """
    resolved = settings or get_settings()
    workspace_id = str(item["workspace_id"])
    notification_id = str(item["notification_id"])
    url = _open(
        item.get(URL_ATTRIBUTE),
        purpose=URL_PURPOSE,
        workspace_id=workspace_id,
        notification_id=notification_id,
        settings=resolved,
    )
    token = None
    if item.get(TOKEN_ATTRIBUTE):
        token = _open(
            item[TOKEN_ATTRIBUTE],
            purpose=TOKEN_PURPOSE,
            workspace_id=workspace_id,
            notification_id=notification_id,
            settings=resolved,
        )
    return url, token


def record_delivery(
    workspace_id: str, notification_id: str, delivery: Mapping[str, Any], *, settings: Settings | None = None
) -> None:
    """Stamp the outcome of the newest delivery, unless the configuration is gone."""
    resolved = settings or get_settings()
    try:
        _repository(resolved).update(
            {"workspace_id": workspace_id, "notification_id": notification_id},
            update_expression="SET #d = :d",
            expression_values={":d": dict(delivery)},
            expression_names={"#d": LAST_DELIVERY_ATTRIBUTE},
            condition=Attr("notification_id").exists(),
        )
    except ConditionFailed:
        return


def render(item: Mapping[str, Any]) -> dict[str, Any]:
    """The API view of one configuration. The URL is masked and the token is never shown."""
    delivery = item.get(LAST_DELIVERY_ATTRIBUTE)
    return {
        "id": str(item["notification_id"]),
        "workspace_id": str(item["workspace_id"]),
        "name": str(item.get("name", "")),
        "destination_type": str(item.get("destination_type", "")),
        "enabled": bool(item.get("enabled", True)),
        "triggers": [str(trigger) for trigger in item.get("triggers") or []],
        "url_masked": str(item.get("url_masked", "")),
        "has_token": bool(item.get(TOKEN_ATTRIBUTE)),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item.get("updated_at", "")),
        "last_delivery": _render_delivery(delivery) if isinstance(delivery, Mapping) else None,
    }


def _render_delivery(delivery: Mapping[str, Any]) -> dict[str, Any]:
    """The last delivery's outcome, with numbers DynamoDB returned as decimals made ints."""
    code = delivery.get("status_code")
    return {
        "status": str(delivery.get("status", "")),
        "trigger": str(delivery.get("trigger", "")),
        "run_id": str(delivery["run_id"]) if delivery.get("run_id") else None,
        "status_code": int(code) if code is not None else None,
        "error": str(delivery["error"]) if delivery.get("error") else None,
        "response_excerpt": str(delivery["response_excerpt"]) if delivery.get("response_excerpt") else None,
        "attempts": int(delivery.get("attempts", 1)),
        "attempted_at": str(delivery.get("attempted_at", "")),
    }


__all__ = [
    "DESTINATION_TYPES",
    "MAX_CONFIGURATIONS_PER_WORKSPACE",
    "NOTIFICATION_ID_PREFIX",
    "DestinationType",
    "InvalidConfiguration",
    "NotificationNotFound",
    "TooManyConfigurations",
    "create_configuration",
    "delete_configuration",
    "delete_workspace_configurations",
    "get_configuration",
    "list_configurations",
    "mask_url",
    "matching_configurations",
    "record_delivery",
    "render",
    "secrets",
    "update_configuration",
    "validate_triggers",
    "validate_url",
]
