"""The workspaces domain's routes: workspaces, variables and config versions.

Every route is guarded by `require_scopes`, so a person behind the JWT authorizer
and an agent holding a `wpk_` key reach them through the same check.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, status

from ...common.core.auth import (
    CONFIGS_READ,
    CONFIGS_WRITE,
    VARIABLES_READ,
    VARIABLES_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
    scopes,
)
from ...common.core.variable_cipher import MasterKeyUnavailable
from . import service
from .schemas.workspace import (
    ConfigVersion,
    ConfigVersionCreate,
    ConfigVersionList,
    ConfigVersionUpload,
    Variable,
    VariableList,
    VariableWrite,
    Workspace,
    WorkspaceCreate,
    WorkspaceList,
    WorkspaceUpdate,
)

router = APIRouter()

WorkspaceId = Path(min_length=4, max_length=64, pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")
ConfigVersionId = Path(min_length=4, max_length=64, pattern=r"^cv-[0-9A-HJKMNP-TV-Z]{26}$")
VariableKey = Path(min_length=1, max_length=256, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _not_found(message: str) -> HTTPException:
    """The 404 every absent row raises, in the shared error envelope's shape."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


@router.get(
    "/workspaces",
    response_model=WorkspaceList,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def list_workspaces() -> dict[str, Any]:
    """Every workspace in this environment."""
    return {"items": service.list_workspaces()}


@router.post(
    "/workspaces",
    response_model=Workspace,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def create_workspace(payload: WorkspaceCreate) -> dict[str, Any]:
    """Create a workspace. The name has to be free."""
    try:
        return service.create_workspace(payload.model_dump())
    except service.WorkspaceNameTaken as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A workspace named '{error}' already exists.",
        ) from error


@router.get(
    "/workspaces/{workspace_id}",
    response_model=Workspace,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def get_workspace(workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """One workspace by id."""
    try:
        return service.get_workspace(workspace_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error


@router.patch(
    "/workspaces/{workspace_id}",
    response_model=Workspace,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def update_workspace(payload: WorkspaceUpdate, workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """Edit one workspace. The name and the id are not editable."""
    try:
        return service.update_workspace(workspace_id, payload.model_dump(exclude_unset=True))
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error


@router.delete(
    "/workspaces/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def delete_workspace(workspace_id: str = WorkspaceId) -> None:
    """Delete one workspace and its variables."""
    try:
        service.delete_workspace(workspace_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error


@router.get(
    "/workspaces/{workspace_id}/variables",
    response_model=VariableList,
    dependencies=[Depends(scopes(VARIABLES_READ))],
)
def list_variables(workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """Every variable on one workspace. A sensitive value is never returned."""
    try:
        items = service.list_variables(workspace_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    return {"items": [service.render_variable(item) for item in items]}


@router.get(
    "/workspaces/{workspace_id}/variables/{key}",
    response_model=Variable,
    dependencies=[Depends(scopes(VARIABLES_READ))],
)
def get_variable(workspace_id: str = WorkspaceId, key: str = VariableKey) -> dict[str, Any]:
    """One variable by key. A sensitive value is never returned."""
    try:
        return service.render_variable(service.get_variable(workspace_id, key))
    except service.VariableNotFound as error:
        raise _not_found("No such variable.") from error


@router.put(
    "/workspaces/{workspace_id}/variables/{key}",
    response_model=Variable,
    dependencies=[Depends(scopes(VARIABLES_WRITE))],
)
def put_variable(
    payload: VariableWrite,
    workspace_id: str = WorkspaceId,
    key: str = VariableKey,
) -> dict[str, Any]:
    """Set one variable. A sensitive value is sealed before it is stored."""
    try:
        stored = service.put_variable(workspace_id, key, payload.model_dump())
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except MasterKeyUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sensitive variables cannot be stored: no encryption key is configured.",
        ) from error
    return service.render_variable(stored)


@router.delete(
    "/workspaces/{workspace_id}/variables/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(scopes(VARIABLES_WRITE))],
)
def delete_variable(workspace_id: str = WorkspaceId, key: str = VariableKey) -> None:
    """Delete one variable."""
    try:
        service.delete_variable(workspace_id, key)
    except service.VariableNotFound as error:
        raise _not_found("No such variable.") from error


@router.post(
    "/workspaces/{workspace_id}/config-versions",
    response_model=ConfigVersionUpload,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(CONFIGS_WRITE))],
)
def create_config_version(
    payload: ConfigVersionCreate,
    workspace_id: str = WorkspaceId,
) -> dict[str, Any]:
    """Create a config version and return the presigned PUT for its tarball."""
    try:
        item, upload = service.create_config_version(workspace_id, payload.size_bytes)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    return {
        "config_version": item,
        "upload_url": upload.url,
        "headers": upload.headers,
        "expires_in": upload.expires_in,
    }


@router.get(
    "/workspaces/{workspace_id}/config-versions",
    response_model=ConfigVersionList,
    dependencies=[Depends(scopes(CONFIGS_READ))],
)
def list_config_versions(workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """One workspace's config versions, oldest first."""
    try:
        return {"items": service.list_config_versions(workspace_id)}
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error


@router.get(
    "/workspaces/{workspace_id}/config-versions/{config_version_id}",
    response_model=ConfigVersion,
    dependencies=[Depends(scopes(CONFIGS_READ))],
)
def get_config_version(
    workspace_id: str = WorkspaceId,
    config_version_id: str = ConfigVersionId,
) -> dict[str, Any]:
    """One config version by id."""
    try:
        return service.get_config_version(workspace_id, config_version_id)
    except service.ConfigVersionNotFound as error:
        raise _not_found("No such config version.") from error
