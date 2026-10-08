"""Remote state sharing: one workspace's non-sensitive outputs, read by another workspace's runs.

HCP Terraform's model: a workspace shares its outputs with every workspace or with a
named list, and a run reads them through its own token. Here the run's API token
carries `state:read-outputs` beside `workspaces:read`, and the source workspace's
`global_remote_state` and `remote_state_consumer_ids` decide whether that run's
workspace may read. A person or agent key holding the scope (an admin) reads any.
Sensitive output values never leave this route.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from webbpulse.identity.api_keys import verify
from webbpulse.identity.scopes import bearer_credential

from ...common.core.auth import STATE_READ_OUTPUTS, WORKSPACES_READ, api_key_store, is_run_api_token, scopes
from . import service, tfe_state
from .router import WorkspaceId
from .schemas.workspace import WorkspaceOutputs
from .state_versions import StateBucketMissing, StateVersionNotFound

REMOTE_STATE_NOT_SHARED_CODE: Final = "REMOTE_STATE_NOT_SHARED"
"""The stable code a run is refused with when the source workspace does not share its outputs with it."""

router = APIRouter()


def caller_workspace_id(request: Request) -> str | None:
    """The workspace a run's API token belongs to, or None for any other caller."""
    presented = bearer_credential(request)
    if not presented:
        return None
    record = verify(presented, api_key_store(), touch=False)
    if record is None or not is_run_api_token(record):
        return None
    return str(record.metadata.get("workspace_id", "") or "")


def shares_with(source: dict[str, Any], consumer_id: str) -> bool:
    """Whether `source` shares its outputs with runs of the workspace `consumer_id`."""
    if source.get("workspace_id") == consumer_id:
        return True
    if source.get("global_remote_state"):
        return True
    return consumer_id in {str(item) for item in source.get("remote_state_consumer_ids") or []}


def _not_shared() -> HTTPException:
    """The 403 a run gets for a workspace that does not share its outputs with it."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "message": "This workspace does not share its outputs with yours. Add yours to its remote state sharing.",
            "error_code": REMOTE_STATE_NOT_SHARED_CODE,
        },
    )


def render_outputs(workspace_id: str, version_id: str | None, found: list[dict[str, Any]]) -> dict[str, Any]:
    """The response body: non-sensitive outputs with values, sensitive ones by name only."""
    return {
        "workspace_id": workspace_id,
        "state_version_id": version_id,
        "outputs": [
            {
                "name": item["name"],
                "type": item["type"],
                "detailed_type": item.get("detailed_type"),
                "value": item.get("value"),
            }
            for item in found
            if not item.get("sensitive")
        ],
        "sensitive_output_names": [item["name"] for item in found if item.get("sensitive")],
    }


@router.get(
    "/workspaces/{workspace_id}/outputs",
    response_model=WorkspaceOutputs,
    dependencies=[Depends(scopes(WORKSPACES_READ, STATE_READ_OUTPUTS))],
)
def read_workspace_outputs(request: Request, response: Response, workspace_id: str = WorkspaceId) -> dict[str, Any]:
    """The non-sensitive outputs of a workspace's current state.

    A run's API token reads only a workspace that is its own, shares globally, or
    names the run's workspace as a consumer; anything else is a 403
    `REMOTE_STATE_NOT_SHARED`. A workspace with no state yet answers an empty list.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        source = service.get_workspace(workspace_id)
    except service.WorkspaceNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.") from error
    consumer = caller_workspace_id(request)
    if consumer is not None and not shares_with(source, consumer):
        raise _not_shared()
    try:
        version_id = tfe_state.current_version_id(workspace_id)
        found = tfe_state.outputs(workspace_id, version_id) if version_id else []
    except service.WorkspaceNotFound as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such workspace.") from error
    except StateVersionNotFound:
        version_id, found = None, []
    except StateBucketMissing as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="State is unavailable: no state bucket is configured.",
        ) from error
    return render_outputs(workspace_id, version_id, found)


__all__ = [
    "REMOTE_STATE_NOT_SHARED_CODE",
    "caller_workspace_id",
    "read_workspace_outputs",
    "render_outputs",
    "router",
    "shares_with",
]
