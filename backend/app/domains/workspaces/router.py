"""The workspaces domain's routes: workspaces, variables and config versions.

Every route is guarded by `require_scopes`, so a person behind the JWT authorizer
and an agent holding a `wpk_` key reach them through the same check.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from webbpulse.identity.claims import AuthorizerClaims

from ...common.core.auth import (
    CONFIGS_READ,
    CONFIGS_WRITE,
    VARIABLES_READ,
    VARIABLES_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
    scopes,
)
from ...common.core.auth import claims as auth_claims
from ...common.core.variable_cipher import MasterKeyUnavailable
from . import hcl, service, state_versions
from .schemas.workspace import (
    ConfigVersion,
    ConfigVersionCreate,
    ConfigVersionList,
    ConfigVersionUpload,
    RunRoleCheck,
    StateVersionDetail,
    StateVersionDownload,
    StateVersionList,
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
StateVersionId = Path(min_length=1, max_length=1024, pattern=r"^[A-Za-z0-9._-]+$")
"""An S3 version id, which is opaque and not a ULID like the ids this API mints.

The pattern is a character allowlist rather than a shape: it keeps a path
separator or a percent escape out of a value that is interpolated into an S3
request, while accepting every id S3 actually issues, including the literal
`null` a version predating versioning carries.
"""
VariableKey = Path(min_length=1, max_length=256, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")


_log = logging.getLogger(__name__)

STATE_DOWNLOAD_EVENT = "workspaces.state_version.download"
"""The log event a state download is recorded under.

The repository has no audit store, so this is a structured log line on the
function's own group rather than a durable audit record, and it is the weakest
part of this feature: it inherits the group's 7 day retention and nothing
enforces that it is written. It is recorded anyway because a state download is
the event most worth reconstructing later, and it names who asked, which
workspace and which version. A real audit trail is a separate decision.
"""


def record_state_download(
    request: Request,
    claims: AuthorizerClaims,
    *,
    workspace_id: str,
    state_version_id: str,
) -> None:
    """Record an authorized download attempt before validation and signing.

    Written ahead of the signing rather than after it so that a failure between
    the two leaves a line that overstates access rather than one that misses it.
    The URL itself is never logged: it is a bearer credential, and a log holding
    it would be a second copy of the thing being protected.
    """
    _log.info(
        "Requested a state version download URL.",
        extra={
            "event": STATE_DOWNLOAD_EVENT,
            "workspace_id": workspace_id,
            "state_version_id": state_version_id,
            "subject": str(claims.get("sub", "") or "") or None,
            "source_ip": request.client.host if request.client else None,
        },
    )


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


@router.get(
    "/workspaces/{workspace_id}/state-versions",
    response_model=StateVersionList,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def list_state_versions(
    workspace_id: str = WorkspaceId,
    page_size: int = Query(default=state_versions.DEFAULT_PAGE_SIZE, ge=1, le=state_versions.MAX_PAGE_SIZE),
    page_token: Optional[str] = Query(default=None, max_length=2048),
) -> dict[str, Any]:
    """One page of a workspace's state history, newest first.

    Guarded by the same scope as reading the workspace itself, so state history
    is reachable by exactly the callers that can already see the workspace and by
    no one else. A workspace that has never run answers an empty page.
    """
    try:
        return state_versions.list_state_versions(
            workspace_id,
            page_size=page_size,
            page_token=page_token,
        )
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except state_versions.StateBucketMissing as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="State history is unavailable: no state bucket is configured.",
        ) from error
    except state_versions.InvalidPageToken as error:
        raise HTTPException(status_code=400, detail="Invalid state history page token.") from error


@router.get(
    "/workspaces/{workspace_id}/state-versions/{state_version_id}",
    response_model=StateVersionDetail,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def get_state_version(
    workspace_id: str = WorkspaceId,
    state_version_id: str = StateVersionId,
) -> dict[str, Any]:
    """One state version's metadata. Never its resources or its outputs."""
    try:
        return state_versions.get_state_version(workspace_id, state_version_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except state_versions.StateVersionNotFound as error:
        raise _not_found("No such state version.") from error
    except state_versions.StateBucketMissing as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="State history is unavailable: no state bucket is configured.",
        ) from error


@router.post(
    "/workspaces/{workspace_id}/state-versions/{state_version_id}/download",
    response_model=StateVersionDownload,
    dependencies=[Depends(scopes(WORKSPACES_READ))],
)
def download_state_version(
    request: Request,
    response: Response,
    workspace_id: str = WorkspaceId,
    state_version_id: str = StateVersionId,
    claims: AuthorizerClaims = Depends(auth_claims),
) -> dict[str, Any]:
    """Mint a short lived URL for one state version's raw bytes.

    A `POST` rather than a `GET` because it is not a read: it mints a bearer
    credential for the most sensitive object the product stores, and that is an
    event worth being a distinct, non cacheable, non prefetchable call.

    The scope guard runs before this function is entered and the service checks
    the version belongs to this workspace before it signs anything, so no URL
    exists until both have passed. The handover is recorded first, because an
    audit line written after the URL is minted would be missing exactly when it
    matters most.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        record_state_download(
            request,
            claims,
            workspace_id=workspace_id,
            state_version_id=state_version_id,
        )
        return state_versions.state_version_download(workspace_id, state_version_id)
    except service.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except state_versions.StateVersionNotFound as error:
        raise _not_found("No such state version.") from error
    except state_versions.StateBucketMissing as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="State history is unavailable: no state bucket is configured.",
        ) from error
