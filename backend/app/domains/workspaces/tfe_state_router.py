"""The `tfe.v2` state routes the cloud backend calls for local state commands.

`terraform state list/show/mv/rm`, `import`, `output` and `state pull/push` read the
current state version, follow its download URL, read outputs and create a new state
version. The download route answers 307 to a presigned S3 GET, because go-tfe sends
its bearer to the download URL and Go drops that header on the cross-host redirect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import RedirectResponse

from ...common.core.auth import STATE_DOWNLOAD, STATE_WRITE, WORKSPACES_READ, scopes
from ...common.tfe.jsonapi import (
    JsonApiRoute,
    conflict,
    document,
    linkage,
    not_found,
    request_attributes,
    resource,
    timestamp,
    unprocessable,
)
from ...common.workspaces.reads import WorkspaceNotFound
from . import tfe_state
from .router import record_state_download
from .state_versions import StateBucketMissing, StateVersionNotFound, state_version_download

if TYPE_CHECKING:  # pragma: no cover
    from webbpulse.identity.claims import AuthorizerClaims

router = APIRouter(prefix="/api/v2", include_in_schema=False, route_class=JsonApiRoute)

WorkspaceId = Path(min_length=4, max_length=64, pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")

StateVersionRef = Path(min_length=30, max_length=1100, pattern=r"^sv-[0-9A-HJKMNP-TV-Z]{26}[A-Za-z0-9._-]+$")

OutputRef = Path(min_length=7, max_length=2048, pattern=r"^wsout-[A-Za-z0-9_-]+$")


def _unavailable(error: Exception) -> HTTPException:
    """The 503 a deployment without a state bucket answers."""
    return HTTPException(status_code=503, detail="State is unavailable: no state bucket is configured.")


def _state_version(workspace_id: str, version_id: str) -> dict[str, Any]:
    """One state version as the HCP `state-versions` resource go-tfe decodes."""
    described = tfe_state.describe(workspace_id, version_id)
    found = tfe_state.outputs(workspace_id, version_id)
    sv_id = tfe_state.state_version_id(workspace_id, version_id)
    attributes = {
        "billable-rum-count": None,
        "created-at": timestamp(described.get("created_at")),
        "hosted-json-state-download-url": "",
        "hosted-json-state-upload-url": "",
        "hosted-state-download-url": f"/api/v2/state-versions/{sv_id}/download",
        "hosted-state-upload-url": "",
        "resources-processed": True,
        "serial": described.get("serial") or 0,
        "size": int(described.get("size_bytes") or 0),
        "state-version": 4,
        "status": "finalized",
        "terraform-version": described.get("terraform_version") or "",
        "vcs-commit-sha": "",
        "vcs-commit-url": "",
    }
    return resource(
        "state-versions",
        sv_id,
        attributes,
        relationships={
            "outputs": {"data": [linkage("state-version-outputs", item["id"])["data"] for item in found]},
            "run": linkage("runs", None),
            "workspace": linkage("workspaces", workspace_id),
        },
        links={"self": f"/api/v2/state-versions/{sv_id}"},
    )


def _output(item: dict[str, Any], *, reveal: bool) -> dict[str, Any]:
    """One output as the HCP `state-version-outputs` resource, a sensitive value only when revealed."""
    hidden = item["sensitive"] and not reveal
    return resource(
        "state-version-outputs",
        item["id"],
        {
            "detailed-type": item["detailed_type"],
            "name": item["name"],
            "sensitive": item["sensitive"],
            "type": item["type"],
            "value": None if hidden else item["value"],
        },
        links={"self": f"/api/v2/state-version-outputs/{item['id']}"},
    )


def _current(workspace_id: str) -> str:
    """The current S3 version id, or the 404 go-tfe reads as "no state yet"."""
    try:
        version_id = tfe_state.current_version_id(workspace_id)
    except WorkspaceNotFound as error:
        raise not_found("workspace") from error
    except StateBucketMissing as error:
        raise _unavailable(error) from error
    if version_id is None:
        raise not_found("state version")
    return version_id


@router.get(
    "/workspaces/{workspace_id}/current-state-version",
    dependencies=[Depends(scopes(WORKSPACES_READ, STATE_DOWNLOAD))],
)
def read_current_state_version(workspace_id: str = WorkspaceId) -> Response:
    """The workspace's current state version, or 404 when it has never had state."""
    version_id = _current(workspace_id)
    try:
        return document(_state_version(workspace_id, version_id))
    except StateVersionNotFound as error:
        raise not_found("state version") from error


@router.get(
    "/state-versions/{state_version_id}",
    dependencies=[Depends(scopes(WORKSPACES_READ, STATE_DOWNLOAD))],
)
def read_state_version(state_version_id: str = StateVersionRef) -> Response:
    """One state version by its `sv-` id."""
    try:
        workspace_id, version_id = tfe_state.parse_state_version_id(state_version_id)
        return document(_state_version(workspace_id, version_id))
    except (WorkspaceNotFound, StateVersionNotFound) as error:
        raise not_found("state version") from error
    except StateBucketMissing as error:
        raise _unavailable(error) from error


@router.get("/state-versions/{state_version_id}/download")
def download_state_version(
    request: Request,
    state_version_id: str = StateVersionRef,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ, STATE_DOWNLOAD)),
) -> Response:
    """Redirect to a 60 second presigned GET of the version's bytes, recorded first.

    The record is the same line the `/api/v1` download writes, ahead of the signing.
    """
    try:
        workspace_id, version_id = tfe_state.parse_state_version_id(state_version_id)
        record_state_download(request, current, workspace_id=workspace_id, state_version_id=version_id)
        minted = state_version_download(workspace_id, version_id)
    except (WorkspaceNotFound, StateVersionNotFound) as error:
        raise not_found("state version") from error
    except StateBucketMissing as error:
        raise _unavailable(error) from error
    return RedirectResponse(minted["download_url"], status_code=307, headers={"Cache-Control": "no-store"})


@router.get(
    "/workspaces/{workspace_id}/current-state-version-outputs",
    dependencies=[Depends(scopes(WORKSPACES_READ, STATE_DOWNLOAD))],
)
def read_current_outputs(workspace_id: str = WorkspaceId) -> Response:
    """Every output of the current state, sensitive values withheld, all on one page.

    The CLI reads only the first page, so nothing is paged away from it.
    """
    version_id = _current(workspace_id)
    try:
        found = tfe_state.outputs(workspace_id, version_id)
    except StateVersionNotFound as error:
        raise not_found("state version") from error
    pagination = {
        "current-page": 1,
        "next-page": None,
        "prev-page": None,
        "total-count": len(found),
        "total-pages": 1,
    }
    return document([_output(item, reveal=False) for item in found], meta={"pagination": pagination})


@router.get("/state-version-outputs/{output_id}")
def read_output(
    request: Request,
    output_id: str = OutputRef,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ, STATE_DOWNLOAD)),
) -> Response:
    """One output with its value, the read `terraform output` makes for a sensitive one.

    A sensitive value leaving here is recorded as a state download.
    """
    try:
        workspace_id, version_id, item = tfe_state.output(output_id)
    except (WorkspaceNotFound, tfe_state.OutputNotFound) as error:
        raise not_found("state version output") from error
    except StateBucketMissing as error:
        raise _unavailable(error) from error
    if item["sensitive"]:
        record_state_download(request, current, workspace_id=workspace_id, state_version_id=version_id)
    return document(_output(item, reveal=True))


@router.post("/workspaces/{workspace_id}/state-versions")
async def create_state_version(
    request: Request,
    workspace_id: str = WorkspaceId,
    current: "AuthorizerClaims" = Depends(scopes(WORKSPACES_READ, STATE_WRITE)),
) -> Response:
    """Write a new state version from inline base64 state, as `terraform state push` and `mv` do.

    A create without `state` is go-tfe's upload path, which this plane does not serve;
    the 422 wording is the one go-tfe answers by retrying with the state inline.
    """
    attributes, _ = await request_attributes(request)
    try:
        version_id = tfe_state.create(workspace_id, str(current.get("sub") or ""), attributes)
        rendered = _state_version(workspace_id, version_id)
    except WorkspaceNotFound as error:
        raise not_found("workspace") from error
    except tfe_state.StateRejected as error:
        raise unprocessable(str(error)) from error
    except tfe_state.StateConflict as error:
        raise conflict(str(error)) from error
    except StateBucketMissing as error:
        raise _unavailable(error) from error
    return document(rendered, status_code=201)
