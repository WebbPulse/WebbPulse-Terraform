"""Workspace notification configurations: HCP Terraform's Settings > Notifications.

Reading needs `workspaces:read`. Every change, and a test delivery, needs
`workspaces:write` and a login within the step-up window, since a configuration
decides where a workspace's run details are sent. A webhook URL is never returned.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, status
from webbpulse.identity.claims import AuthorizerClaims
from webbpulse.ratelimit import RateLimiter

from ...common.composition.settings import Settings, get_settings
from ...common.core.auth import WORKSPACES_READ, WORKSPACES_WRITE, run_api_workspace_binding, scopes, sudo
from ...common.core.auth import claims as auth_claims
from ...common.core.variable_cipher import MasterKeyUnavailable
from ...common.notifications import delivery, store
from . import service
from .router import WorkspaceId
from .schemas.notification import (
    NotificationConfiguration,
    NotificationConfigurationCreate,
    NotificationConfigurationList,
    NotificationConfigurationUpdate,
    NotificationDelivery,
)

router = APIRouter(dependencies=[Depends(run_api_workspace_binding)])

NotificationId = Path(min_length=4, max_length=64, pattern=r"^nc-[0-9A-HJKMNP-TV-Z]{26}$")

VERIFY_LIMIT: int = 20
"""Test deliveries per configuration per minute, under Discord's 30 a minute per webhook."""

VERIFY_WINDOW_SECONDS: int = 60

CHANGE_EVENT = "workspaces.notification.change"
"""The log event a configuration change is recorded under, naming who, which and what."""

_log = logging.getLogger(__name__)


def _no_workspace() -> HTTPException:
    """The 404 for an absent workspace."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.")


def _no_configuration() -> HTTPException:
    """The 404 for an absent configuration."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such notification configuration.")


def _unprocessable(error: Exception) -> HTTPException:
    """The 422 for a refused field. The message never quotes the URL or the token."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error))


def _no_key() -> HTTPException:
    """The 503 when nothing can seal the URL, rather than storing it in the clear."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Notification configurations cannot be stored: no encryption key is configured.",
    )


def _workspace(workspace_id: str) -> dict[str, Any]:
    """The workspace, or the 404."""
    try:
        return service.get_workspace(workspace_id)
    except service.WorkspaceNotFound as error:
        raise _no_workspace() from error


def _record_change(current: AuthorizerClaims, action: str, workspace_id: str, notification_id: str) -> None:
    """Log one configuration change with who made it. Neither secret is ever in it."""
    _log.info(
        "Changed a notification configuration.",
        extra={
            "event": CHANGE_EVENT,
            "action": action,
            "workspace_id": workspace_id,
            "notification_id": notification_id,
            "actor": str(current.get("sub", "") or ""),
        },
    )


def _limiter(settings: Settings) -> RateLimiter:
    """The test delivery limiter, on the stack's shared rate limit table."""
    prefix = settings.IDENTITY_TABLE_PREFIX.strip() or f"webbpulse-terraform-{settings.ENVIRONMENT}"
    return RateLimiter(
        namespace="notification-verify",
        prefix=prefix,
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.dynamodb_endpoint_url,
    )


@router.get(
    "/workspaces/{workspace_id}/notification-configurations",
    response_model=NotificationConfigurationList,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def list_notification_configurations(workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """Every notification configuration on one workspace, with its last delivery."""
    _workspace(workspace_id)
    return {"items": [store.render(item) for item in store.list_configurations(workspace_id)]}


@router.post(
    "/workspaces/{workspace_id}/notification-configurations",
    response_model=NotificationConfiguration,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(sudo(WORKSPACES_WRITE))],
)
def create_notification_configuration(
    payload: NotificationConfigurationCreate,
    workspace_id: str = WorkspaceId,
    current: AuthorizerClaims = Depends(auth_claims),
) -> dict[str, Any]:
    """Add a notification configuration.

    `slack` takes an incoming webhook URL on `hooks.slack.com`, `discord` a channel
    webhook URL on `discord.com`, and `generic` any HTTPS URL, which receives HCP
    Terraform's version 1 payload signed with `token` in `X-TFE-Notification-Signature`.
    A workspace holds at most 50.
    """
    _workspace(workspace_id)
    body = payload.model_dump()
    body["url"] = payload.url.get_secret_value()
    body["token"] = payload.token.get_secret_value() if payload.token is not None else None
    try:
        created = store.create_configuration(workspace_id, body)
    except store.InvalidConfiguration as error:
        raise _unprocessable(error) from error
    except store.TooManyConfigurations as error:
        limit = store.MAX_CONFIGURATIONS_PER_WORKSPACE
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A workspace can hold at most {limit} notification configurations.",
        ) from error
    except MasterKeyUnavailable as error:
        raise _no_key() from error
    _record_change(current, "create", workspace_id, str(created["notification_id"]))
    return store.render(created)


@router.get(
    "/workspaces/{workspace_id}/notification-configurations/{notification_id}",
    response_model=NotificationConfiguration,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def get_notification_configuration(
    workspace_id: str = WorkspaceId, notification_id: str = NotificationId
) -> dict[str, Any]:
    """One notification configuration, with its last delivery."""
    try:
        return store.render(store.get_configuration(workspace_id, notification_id))
    except store.NotificationNotFound as error:
        raise _no_configuration() from error


@router.patch(
    "/workspaces/{workspace_id}/notification-configurations/{notification_id}",
    response_model=NotificationConfiguration,
    dependencies=[Depends(sudo(WORKSPACES_WRITE))],
)
def update_notification_configuration(
    payload: NotificationConfigurationUpdate,
    workspace_id: str = WorkspaceId,
    notification_id: str = NotificationId,
    current: AuthorizerClaims = Depends(auth_claims),
) -> dict[str, Any]:
    """Edit, enable or disable a notification configuration. An absent field is unchanged."""
    changes = payload.model_dump(exclude_unset=True)
    if "url" in changes:
        changes["url"] = payload.url.get_secret_value() if payload.url is not None else None
    if "token" in changes:
        changes["token"] = payload.token.get_secret_value() if payload.token is not None else None
    try:
        updated = store.update_configuration(workspace_id, notification_id, changes)
    except store.NotificationNotFound as error:
        raise _no_configuration() from error
    except store.InvalidConfiguration as error:
        raise _unprocessable(error) from error
    except MasterKeyUnavailable as error:
        raise _no_key() from error
    _record_change(current, "update", workspace_id, notification_id)
    return store.render(updated)


@router.delete(
    "/workspaces/{workspace_id}/notification-configurations/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(sudo(WORKSPACES_WRITE))],
)
def delete_notification_configuration(
    workspace_id: str = WorkspaceId,
    notification_id: str = NotificationId,
    current: AuthorizerClaims = Depends(auth_claims),
) -> None:
    """Delete a notification configuration."""
    try:
        store.delete_configuration(workspace_id, notification_id)
    except store.NotificationNotFound as error:
        raise _no_configuration() from error
    _record_change(current, "delete", workspace_id, notification_id)


@router.post(
    "/workspaces/{workspace_id}/notification-configurations/{notification_id}/actions/verify",
    response_model=NotificationDelivery,
    dependencies=[Depends(sudo(WORKSPACES_WRITE))],
)
def verify_notification_configuration(
    workspace_id: str = WorkspaceId,
    notification_id: str = NotificationId,
) -> dict[str, Any]:
    """Send a test delivery now, as HCP's Send test does, and return its outcome.

    The delivery carries the trigger `verification` and no run. It is sent even when
    the configuration is disabled. A receiver's refusal is reported in the body with
    a 200, since the request itself succeeded; 20 a minute per configuration.
    """
    workspace = _workspace(workspace_id)
    try:
        configuration = store.get_configuration(workspace_id, notification_id)
    except store.NotificationNotFound as error:
        raise _no_configuration() from error
    settings = get_settings()
    decision = _limiter(settings).check(notification_id, limit=VERIFY_LIMIT, window_seconds=VERIFY_WINDOW_SECONDS)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many test deliveries for this configuration. Try again in a minute.",
            headers={"Retry-After": str(max(1, int(decision.reset_after)))},
        )
    return store.render({**configuration, "last_delivery": delivery.verify(configuration, workspace)})["last_delivery"]


__all__ = ["router"]
