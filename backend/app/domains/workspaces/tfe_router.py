"""The workspaces function's share of `tfe.v2`: ping, workspaces, their locks and config versions.

These are the calls `terraform init` makes through a `cloud {}` block before any run
exists: go-tfe's ping, the organization's entitlements, the workspace by name and,
for local operations such as `import`, the workspace's variables. Every route answers
in HCP's JSON:API shapes, and every failure as a JSON:API error document.

Served at `/api/v2` behind no authorizer, like the registry protocols, because the
CLI sends the `terraform login` key as a bearer token; the `wpk_` key is verified in
process by the same claims dependency every other route uses.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from webbpulse.identity.scopes import claims_scopes

from ...common.core.auth import (
    CONFIGS_READ,
    CONFIGS_WRITE,
    STATE_WRITE,
    VARIABLES_READ,
    WORKSPACES_READ,
    bound_workspace_id,
    claims,
    run_api_workspace_binding,
    scopes,
)
from ...common.tfe.jsonapi import (
    JsonApiRoute,
    conflict,
    document,
    not_found,
    paginate,
    request_attributes,
    unprocessable,
)
from ...common.tfe.resources import (
    API_VERSION,
    configuration_version_resource,
    entitlement_set,
    require_organization,
    variable_resource,
    workspace_resource,
)
from ...common.workspaces.reads import ConfigVersionNotFound, WorkspaceNotFound
from ...common.workspaces.reads import get_workspace as read_workspace
from . import locks
from .service import (
    create_tfe_config_version,
    find_by_name,
    find_config_version,
    list_variables,
    list_workspaces,
    render_variable,
)

if TYPE_CHECKING:  # pragma: no cover
    from webbpulse.identity.claims import AuthorizerClaims

router = APIRouter(
    prefix="/api/v2",
    include_in_schema=False,
    route_class=JsonApiRoute,
    dependencies=[Depends(run_api_workspace_binding)],
)

WorkspaceId = Path(min_length=4, max_length=64, pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")

WorkspaceName = Path(min_length=1, max_length=90)

ConfigVersionId = Path(min_length=4, max_length=64, pattern=r"^cv-[0-9A-HJKMNP-TV-Z]{26}$")


def _render(item: Mapping[str, Any], held: Any) -> dict[str, Any]:
    """One workspace resource, `locked` read from the lockfile the runs share."""
    workspace_id = str(item["workspace_id"])
    return workspace_resource(item, held, locked=locks.is_locked(workspace_id))


@router.get("/ping", status_code=204)
def ping() -> Response:
    """go-tfe's handshake, answering the API version the cloud backend checks.

    `TFP-AppName` is left unset: go-tfe accepts only HCP's own two names there and
    otherwise falls back to its default, which is what an unset header gets too.
    """
    return Response(status_code=204, headers={"TFP-API-Version": API_VERSION})


@router.get("/organizations/{organization}/entitlement-set")
def read_entitlements(
    organization: str,
    _: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ)),
) -> Response:
    """The organization's entitlements, read by every `cloud {}` configure."""
    require_organization(organization)
    return document(entitlement_set())


@router.get("/organizations/{organization}/workspaces/{name}")
def read_workspace_by_name(
    organization: str,
    request: Request,
    name: str = WorkspaceName,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ)),
) -> Response:
    """One workspace by name, which is how the `cloud {}` block names it.

    A run's API token finds only its own workspace; any other name is not found.
    """
    require_organization(organization)
    item = find_by_name(name)
    bound = bound_workspace_id(request)
    if item is not None and bound is not None and str(item.get("workspace_id", "")) != bound:
        item = None
    if item is None:
        raise not_found("workspace")
    return document(_render(item, claims_scopes(current)))


@router.get("/organizations/{organization}/workspaces")
def list_organization_workspaces(
    organization: str,
    request: Request,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ)),
) -> Response:
    """The organization's workspaces, filtered by `search[name]` and paginated.

    Workspaces carry no tags, so a `search[tags]` or `filter[tagged]` query matches
    none, which is what a `cloud {}` block selecting by tags sees. A run's API token
    sees only its own workspace.
    """
    require_organization(organization)
    params = request.query_params
    items = list_workspaces()
    bound = bound_workspace_id(request)
    if bound is not None:
        items = [item for item in items if str(item.get("workspace_id", "")) == bound]
    if params.get("search[tags]") or any(key.startswith("filter[tagged]") for key in params):
        items = []
    needle = (params.get("search[name]") or "").lower()
    if needle:
        items = [item for item in items if needle in str(item.get("name", "")).lower()]
    items = sorted(items, key=lambda item: str(item.get("name", "")))
    page, meta = paginate(items, request)
    held = claims_scopes(current)
    return document([_render(item, held) for item in page], meta=meta)


@router.post("/organizations/{organization}/workspaces")
async def create_organization_workspace(
    request: Request,
    organization: str,
    _: "AuthorizerClaims" = Depends(claims),
) -> Response:
    """Refuse to create a workspace from the CLI, naming the workspace and what to do.

    The cloud backend creates a workspace it cannot find. A workspace here needs a run
    role and a reviewed engine version, so it is created in the UI or through
    `/api/v1/workspaces`, and the CLI shows this message instead. Any valid key gets
    the message, since a `terraform login` key carries no `workspaces:write` and a
    scope error would not say what to do.
    """
    require_organization(organization)
    attributes, _relationships = await request_attributes(request)
    name = str(attributes.get("name") or "").strip()
    named = f'Workspace "{name}" does not exist. ' if name else ""
    raise unprocessable(
        f"{named}Workspaces are not created from the CLI. "
        "Create it in the WebbPulse Terraform UI, then run terraform init again."
    )


@router.get("/workspaces/{workspace_id}")
def read_workspace_by_id(
    workspace_id: str = WorkspaceId,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ)),
) -> Response:
    """One workspace by id."""
    try:
        item = read_workspace(workspace_id)
    except WorkspaceNotFound as error:
        raise not_found("workspace") from error
    return document(_render(item, claims_scopes(current)))


@router.get("/workspaces/{workspace_id}/all-vars")
def list_all_variables(
    request: Request,
    workspace_id: str = WorkspaceId,
    _: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ, VARIABLES_READ)),
) -> Response:
    """The workspace's variables, sensitive values null, as local operations read them."""
    try:
        rows = list_variables(workspace_id)
    except WorkspaceNotFound as error:
        raise not_found("workspace") from error
    rendered: list[dict[str, Any]] = [variable_resource(render_variable(row)) for row in rows]
    page, meta = paginate(rendered, request)
    return document(page, meta=meta)


@router.post("/workspaces/{workspace_id}/configuration-versions", status_code=201)
async def create_configuration_version(
    request: Request,
    workspace_id: str = WorkspaceId,
    _: "AuthorizerClaims" = Depends(scopes(CONFIGS_WRITE)),
) -> Response:
    """Store a pending config version and answer its `upload-url`, as `terraform plan` asks.

    The URL is a presigned S3 PUT that signs no content type or length, because go-tfe
    uploads with no Authorization and `application/octet-stream`; the size ceiling is
    enforced when the version is read back.
    """
    attributes, _relationships = await request_attributes(request)
    try:
        item, upload_url = create_tfe_config_version(
            workspace_id,
            speculative=bool(attributes.get("speculative", False)),
            auto_queue_runs=bool(attributes.get("auto-queue-runs", True)),
            provisional=attributes.get("provisional") is True,
        )
    except WorkspaceNotFound as error:
        raise not_found("workspace") from error
    return document(configuration_version_resource(item, upload_url=upload_url), status_code=201)


@router.get("/configuration-versions/{config_version_id}")
def read_configuration_version(
    config_version_id: str = ConfigVersionId,
    _: "AuthorizerClaims" = Depends(scopes(CONFIGS_READ)),
) -> Response:
    """One config version, reconciled against its object, which the upload poll reads."""
    try:
        item = find_config_version(config_version_id)
    except ConfigVersionNotFound as error:
        raise not_found("configuration version") from error
    return document(configuration_version_resource(item))


async def _lock_reason(request: Request) -> str:
    """The `reason` go-tfe sends with a lock, read leniently since it is only shown."""
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return ""
    if not isinstance(body, Mapping):
        return ""
    data = body.get("data")
    attributes = data.get("attributes") if isinstance(data, Mapping) else None
    reason = (attributes or {}).get("reason") if isinstance(attributes, Mapping) else body.get("reason")
    return str(reason or "")[:500]


def _workspace_or_404(workspace_id: str) -> dict[str, Any]:
    """The workspace row, or the JSON:API 404."""
    try:
        return read_workspace(workspace_id)
    except WorkspaceNotFound as error:
        raise not_found("workspace") from error


@router.post("/workspaces/{workspace_id}/actions/lock")
async def lock_workspace(
    request: Request,
    workspace_id: str = WorkspaceId,
    current: "AuthorizerClaims" = Depends(scopes(STATE_WRITE)),
) -> Response:
    """Lock the workspace for a local state operation, through the runs' own lockfile.

    Any 409 here is go-tfe's `ErrWorkspaceLocked`, which the CLI reports with the lock ID
    `force-unlock` takes.
    """
    item = _workspace_or_404(workspace_id)
    reason = await _lock_reason(request)
    try:
        locks.lock(workspace_id, str(current.get("sub") or ""), reason)
    except locks.LockedByRun as error:
        raise conflict(f"Unable to lock workspace. The workspace is locked by Run {error}.") from error
    except locks.WorkspaceLocked as error:
        raise conflict("Unable to lock workspace. The workspace is already locked.") from error
    return document(workspace_resource(item, claims_scopes(current), locked=True))


@router.post("/workspaces/{workspace_id}/actions/unlock")
def unlock_workspace(
    workspace_id: str = WorkspaceId,
    current: "AuthorizerClaims" = Depends(scopes(STATE_WRITE)),
) -> Response:
    """Unlock a workspace this caller locked. go-tfe maps the 409's wording to its errors."""
    item = _workspace_or_404(workspace_id)
    try:
        locks.unlock(workspace_id, str(current.get("sub") or ""))
    except locks.WorkspaceNotLocked as error:
        raise conflict("Unable to unlock workspace. The workspace is not locked.") from error
    except locks.LockedByRun as error:
        raise conflict("Unable to unlock workspace. The workspace is locked by Run.") from error
    except locks.LockedByOther as error:
        raise conflict(f"Unable to unlock workspace. The workspace is locked by User {error.holder}.") from error
    return document(workspace_resource(item, claims_scopes(current), locked=False))


@router.post("/workspaces/{workspace_id}/actions/force-unlock")
def force_unlock_workspace(
    workspace_id: str = WorkspaceId,
    current: "AuthorizerClaims" = Depends(scopes(STATE_WRITE)),
) -> Response:
    """Remove any lock, a crashed run's included, unless a run is still going.

    `terraform force-unlock` with the `WebbPulse/<name>` lock ID reaches here, which is
    the way past a stale lockfile without break-glass.
    """
    item = _workspace_or_404(workspace_id)
    try:
        locks.force_unlock(workspace_id)
    except locks.WorkspaceNotLocked as error:
        raise conflict("Unable to force-unlock workspace. The workspace is not locked.") from error
    except locks.LockedByRun as error:
        raise conflict(f"Unable to force-unlock workspace. Run {error} is still going.") from error
    return document(workspace_resource(item, claims_scopes(current), locked=False))
