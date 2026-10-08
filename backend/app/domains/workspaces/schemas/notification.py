"""Request and response models for workspace notification configurations.

The URL and the token are `SecretStr`, so neither appears in a model's repr or a log
line, and they carry no length or pattern constraint here: the service checks them
and its messages never quote the value, where a validation error would echo it.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ....common.notifications.store import MAX_NAME_LENGTH
from ....common.notifications.triggers import Trigger

DestinationType = Literal["slack", "discord", "generic"]
"""Where deliveries go: a Slack incoming webhook, a Discord channel webhook, or any HTTPS endpoint."""

DeliveryStatus = Literal["succeeded", "failed", "retrying"]


class NotificationConfigurationCreate(BaseModel):
    """A new notification configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    destination_type: DestinationType
    url: SecretStr
    """The webhook URL. Stored sealed and never returned; responses show `url_masked`."""
    token: Optional[SecretStr] = None
    """A generic webhook's signing token, which HMAC-SHA512 signs each body with. Never returned."""
    enabled: bool = True
    triggers: list[Trigger] = Field(default_factory=list)


class NotificationConfigurationUpdate(BaseModel):
    """A partial edit. An absent field is unchanged; a null or empty `token` clears it."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=MAX_NAME_LENGTH)
    destination_type: Optional[DestinationType] = None
    url: Optional[SecretStr] = None
    """A new webhook URL. Changing `destination_type` needs one."""
    token: Optional[SecretStr] = None
    enabled: Optional[bool] = None
    triggers: Optional[list[Trigger]] = None


class NotificationDelivery(BaseModel):
    """The outcome of a configuration's newest delivery attempt."""

    status: DeliveryStatus
    trigger: str
    """The trigger it carried, or `verification` for a test."""
    run_id: Optional[str] = None
    status_code: Optional[int] = None
    """The receiver's HTTP status, absent when no answer came back."""
    error: Optional[str] = None
    """What went wrong, as a category such as `timeout` or `blocked_address`, never the URL."""
    response_excerpt: Optional[str] = None
    """The start of the receiver's answer, at most 512 characters."""
    attempts: int
    attempted_at: str


class NotificationConfiguration(BaseModel):
    """A stored configuration as the API renders it, with the URL masked."""

    id: str
    workspace_id: str
    name: str
    destination_type: DestinationType
    enabled: bool
    triggers: list[Trigger]
    url_masked: str
    """The URL's scheme and host with the path hidden, such as `https://hooks.slack.com/****`."""
    has_token: bool
    created_at: str
    updated_at: str
    last_delivery: Optional[NotificationDelivery] = None


class NotificationConfigurationList(BaseModel):
    """Every notification configuration on one workspace."""

    items: list[NotificationConfiguration]


__all__ = [
    "DeliveryStatus",
    "DestinationType",
    "NotificationConfiguration",
    "NotificationConfigurationCreate",
    "NotificationConfigurationList",
    "NotificationConfigurationUpdate",
    "NotificationDelivery",
]
