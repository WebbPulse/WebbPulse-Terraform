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
from . import hcl, service
from .schemas.workspace import (
    ConfigVersion,
    ConfigVersionCreate,
    ConfigVersionList,
    ConfigVersionUpload,
    RunRoleCheck,
    Variable,
    VariableList,
    VariableWrite,
    Workspace,
    WorkspaceCreate,
    WorkspaceList,
    WorkspaceUpdate,
)

RUN_ROLE_MISSING_CODE = "RUN_ROLE_MISSING"
"""The stable code a caller matches on when a workspace has no run role yet."""

router = APIRouter()

WorkspaceId = Path(min_length=4, max_length=64, pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")
ConfigVersionId = Path(min_length=4, max_length=64, pattern=r"^cv-[0-9A-HJKMNP-TV-Z]{26}$")
VariableKey = Path(min_length=1, max_length=256, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _not_found(message: str) -> HTTPException:
    """The 404 every absent row raises, in the shared error envelope's shape."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


def _run_role_missing() -> HTTPException:
    """The 400 both run role check routes raise when no ARN is configured."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "message": "This workspace has no run role ARN yet.",
            "error_code": RUN_ROLE_MISSING_CODE,
        },
    )


@router.get(
    "/workspaces",
    response_model=WorkspaceList,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def list_workspaces() -> dict[str, Any]:
    """Every workspace in this environment, each with its run role setup."""
    return {"items": [service.render_workspace(item) for item in service.list_workspaces()]}


@router.post(
    "/workspaces",
    response_model=Workspace,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def create_workspace(payload: WorkspaceCreate) -> dict[str, Any]:
    """Create a workspace. The name has to be free.

    The run role is optional here on purpose: its trust policy names the workspace
    id as the external id, so the role cannot exist until the workspace does. The
    response's `run_role_setup` carries everything needed to build it, and
    `PATCH /workspaces/{id}` attaches it afterwards.
    """
    try:
        return service.render_workspace(service.create_workspace(payload.model_dump()))
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
    """One workspace by id, with its run role setup."""
    try:
        return service.render_workspace(service.get_workspace(workspace_id))
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error


@router.patch(
    "/workspaces/{workspace_id}",
    response_model=Workspace,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def update_workspace(payload: WorkspaceUpdate, workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """Edit one workspace. The name and the id are not editable.

    The body is JSON Merge Patch: an omitted key leaves the stored value exactly
    as it was, and an explicit null on `run_role_arn`, `working_directory` or
    `description` clears that field. `model_dump(exclude_unset=True)` is what keeps
    the two apart, so a field is only touched when the request carried its key.

    Changing or clearing `run_role_arn` drops the recorded check outcome, so the
    role reads as unchecked until `run-role/check` says otherwise.
    """
    try:
        updated = service.update_workspace(workspace_id, payload.model_dump(exclude_unset=True))
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    return service.render_workspace(updated)


@router.get(
    "/workspaces/{workspace_id}/run-role/check",
    response_model=RunRoleCheck,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def read_run_role_check(workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """Assume the workspace's run role and report whether it answered, writing nothing.

    The same probe as the POST, without the record it leaves behind. A caller that
    only wants to look, such as a Terraform provider reading on every plan and
    refresh, uses this one so no plan mutates a workspace row. Because nothing is
    written, the read scope is enough.

    Always 200 when a role is configured, whether or not it answered. A workspace
    with no role at all is a 400 carrying `RUN_ROLE_MISSING`.
    """
    try:
        return service.probe_run_role(workspace_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except service.RunRoleMissing as error:
        raise _run_role_missing() from error


@router.post(
    "/workspaces/{workspace_id}/run-role/check",
    response_model=RunRoleCheck,
    dependencies=[Depends(scopes(WORKSPACES_WRITE))],
)
def check_run_role(workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """Assume the workspace's run role and record the outcome on the workspace.

    The recorded outcome is what the setup UI shows between visits, so this route
    keeps its write and its write scope. Use the GET when only the answer is
    wanted.

    Always 200 when a role is configured, whether or not it answered: a trust
    policy that is not there yet is an expected state of the setup rather than a
    request error. A workspace with no role at all is a 400 carrying
    `RUN_ROLE_MISSING`.
    """
    try:
        return service.check_run_role(workspace_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except service.RunRoleMissing as error:
        raise _run_role_missing() from error


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
    """Set one variable. A sensitive value is sealed before it is stored.

    A broken HCL expression and an `env` variable marked HCL are both refused
    here, so neither is stored to fail on every later run.
    """
    try:
        stored = service.put_variable(workspace_id, key, payload.model_dump())
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except service.HclNotAllowed as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "An env variable cannot be HCL: an environment variable is a string to "
                "the process, so there is nothing to parse the expression. Set hcl to "
                "false, or set category to terraform."
            ),
        ) from error
    except hcl.InvalidHcl as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The value is not a usable HCL expression. {error}",
        ) from error
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
