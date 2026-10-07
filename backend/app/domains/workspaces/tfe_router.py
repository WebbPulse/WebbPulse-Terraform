"""The workspaces function's share of `tfe.v2`: ping, organization and workspace reads.

These are the calls `terraform init` makes through a `cloud {}` block before any run
exists: go-tfe's ping, the organization's entitlements, the workspace by name and,
for local operations such as `import`, the workspace's variables. Every route answers
in HCP's JSON:API shapes, and every failure as a JSON:API error document.

Served at `/api/v2` behind no authorizer, like the registry protocols, because the
CLI sends the `terraform login` key as a bearer token; the `wpk_` key is verified in
process by the same claims dependency every other route uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from webbpulse.identity.scopes import claims_scopes

from ...common.core.auth import VARIABLES_READ, WORKSPACES_READ, WORKSPACES_WRITE, scopes
from ...common.tfe.jsonapi import JsonApiRoute, document, not_found, paginate, unprocessable
from ...common.tfe.resources import (
    API_VERSION,
    entitlement_set,
    require_organization,
    variable_resource,
    workspace_resource,
)
from ...common.workspaces.reads import WorkspaceNotFound
from ...common.workspaces.reads import get_workspace as read_workspace
from .service import find_by_name, list_variables, list_workspaces, render_variable

if TYPE_CHECKING:  # pragma: no cover
    from webbpulse.identity.claims import AuthorizerClaims

router = APIRouter(prefix="/api/v2", include_in_schema=False, route_class=JsonApiRoute)

WorkspaceId = Path(min_length=4, max_length=64, pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")

WorkspaceName = Path(min_length=1, max_length=90)


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
    name: str = WorkspaceName,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ)),
) -> Response:
    """One workspace by name, which is how the `cloud {}` block names it."""
    require_organization(organization)
    item = find_by_name(name)
    if item is None:
        raise not_found("workspace")
    return document(workspace_resource(item, claims_scopes(current)))


@router.get("/organizations/{organization}/workspaces")
def list_organization_workspaces(
    organization: str,
    request: Request,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ)),
) -> Response:
    """The organization's workspaces, filtered by `search[name]` and paginated.

    Workspaces carry no tags, so a `search[tags]` or `filter[tagged]` query matches
    none, which is what a `cloud {}` block selecting by tags sees.
    """
    require_organization(organization)
    params = request.query_params
    items = list_workspaces()
    if params.get("search[tags]") or any(key.startswith("filter[tagged]") for key in params):
        items = []
    needle = (params.get("search[name]") or "").lower()
    if needle:
        items = [item for item in items if needle in str(item.get("name", "")).lower()]
    items = sorted(items, key=lambda item: str(item.get("name", "")))
    page, meta = paginate(items, request)
    held = claims_scopes(current)
    return document([workspace_resource(item, held) for item in page], meta=meta)


@router.post("/organizations/{organization}/workspaces")
def create_organization_workspace(
    organization: str,
    _: "AuthorizerClaims" = Depends(scopes(WORKSPACES_WRITE)),
) -> Response:
    """Refuse to create a workspace from the CLI.

    The cloud backend creates a workspace it cannot find. A workspace here needs a run
    role and a reviewed engine version, so it is created in the UI or through
    `/api/v1/workspaces`, and the CLI shows this message instead.
    """
    require_organization(organization)
    raise unprocessable(
        "Workspaces are not created from the CLI. "
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
    return document(workspace_resource(item, claims_scopes(current)))


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
