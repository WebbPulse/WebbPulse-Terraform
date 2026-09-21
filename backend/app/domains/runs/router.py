"""The runs domain's routes, including the three the runner owns.

Eleven routes on two different credentials. Eight are guarded by
`require_scopes` and reached by a person through the JWT authorizer or an agent
through a `wpk_` key. Three, the bundle, the artifact upload and the phase
result, are guarded by a run token bound to the run in the path, because the
bundle carries decrypted variables and no human scope should open it.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from ...common.core.auth import (
    RUNS_APPLY,
    RUNS_READ,
    RUNS_WRITE,
    require_run_token,
    scopes,
)
from ...common.workspaces import reads as workspace_reads
from . import service
from .schemas.run import (
    ArtifactUpload,
    ArtifactUploadCreate,
    LogPage,
    PhaseResult,
    PhaseResultAccepted,
    Run,
    RunBundle,
    RunCreate,
    RunCreated,
    RunList,
)

router = APIRouter()

RunId = Path(min_length=4, max_length=64, pattern=r"^run-[0-9A-HJKMNP-TV-Z]{26}$")
WorkspaceIdQuery = Query(min_length=4, max_length=64, pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$")


def _not_found(message: str) -> HTTPException:
    """The 404 every absent row raises."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


def _conflict(message: str, *, error_code: str | None = None) -> HTTPException:
    """The 409 every refused transition raises, optionally carrying a stable code."""
    detail: Any = message if error_code is None else {"message": message, "error_code": error_code}
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


RUN_ROLE_MISSING_CODE = "RUN_ROLE_MISSING"
"""The code a caller matches on to send a person to the workspace's run role setup."""


@router.post(
    "/runs",
    response_model=RunCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(RUNS_WRITE))],
)
def create_run(payload: RunCreate) -> dict[str, Any]:
    """Queue or start a run.

    A run queued behind the workspace's active one comes back `pending` with
    `queued_behind` set and no token, which is the caller's signal that nothing is
    executing yet.

    A workspace with no run role is a 409 carrying `RUN_ROLE_MISSING`, since the
    runner would have nothing to assume.
    """
    try:
        created = service.create_run(payload.model_dump())
    except workspace_reads.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except workspace_reads.RunRoleMissing as error:
        raise _conflict(
            "This workspace has no run role ARN yet, so a run has nothing to assume.",
            error_code=RUN_ROLE_MISSING_CODE,
        ) from error
    except workspace_reads.ConfigVersionNotFound as error:
        raise _not_found("No such config version.") from error
    except service.ConfigVersionNotReady as error:
        raise _conflict("That config version has no uploaded configuration.") from error
    return service.render_run(created) | {"run_token": created.get("run_token")}


@router.get(
    "/runs",
    response_model=RunList,
    dependencies=[Depends(scopes(RUNS_READ))],
)
def list_runs(workspace_id: str = WorkspaceIdQuery) -> dict[str, Any]:
    """One workspace's runs, newest first. The workspace is required."""
    try:
        items = service.list_runs(workspace_id)
    except workspace_reads.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    return {"items": [service.render_run(item) for item in items]}


@router.get(
    "/runs/{run_id}",
    response_model=Run,
    dependencies=[Depends(scopes(RUNS_READ))],
)
def get_run(run_id: str = RunId) -> dict[str, Any]:
    """One run by id."""
    try:
        return service.render_run(service.get_run(run_id))
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error


@router.post(
    "/runs/{run_id}/confirm",
    response_model=Run,
    dependencies=[Depends(scopes(RUNS_APPLY))],
)
def confirm_run(run_id: str = RunId) -> dict[str, Any]:
    """Apply a planned run. Needs `runs:apply`, not `runs:write`."""
    try:
        return service.render_run(service.confirm_run(run_id))
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.RunNotConfirmable as error:
        raise _conflict("That run is not awaiting a confirmation.") from error


@router.post(
    "/runs/{run_id}/cancel",
    response_model=Run,
    dependencies=[Depends(scopes(RUNS_WRITE))],
)
def cancel_run(run_id: str = RunId) -> dict[str, Any]:
    """Cancel a run, stopping its execution if one is running."""
    try:
        return service.render_run(service.cancel_run(run_id))
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.RunNotCancellable as error:
        raise _conflict("That run already finished.") from error


@router.post(
    "/runs/{run_id}/discard",
    response_model=Run,
    dependencies=[Depends(scopes(RUNS_WRITE))],
)
def discard_run(run_id: str = RunId) -> dict[str, Any]:
    """Drop a plan that was never applied, ending its execution cleanly."""
    try:
        return service.render_run(service.discard_run(run_id))
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.RunNotDiscardable as error:
        raise _conflict("That run has no plan awaiting a decision.") from error


@router.get(
    "/runs/{run_id}/logs",
    response_model=LogPage,
    dependencies=[Depends(scopes(RUNS_READ))],
)
def run_logs(
    run_id: str = RunId,
    phase: str = Query(default="plan", pattern=r"^(plan|apply)$"),
    after: Optional[str] = Query(default=None, max_length=2048),
) -> dict[str, Any]:
    """One page of a phase's logs. Pass `next_after` back as `after` to continue."""
    try:
        return service.run_logs(run_id, "apply" if phase == "apply" else "plan", after)
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error


@router.get(
    "/runs/{run_id}/bundle",
    response_model=RunBundle,
    dependencies=[Depends(require_run_token())],
)
def run_bundle(run_id: str = RunId) -> dict[str, Any]:
    """Everything the runner needs for this run's current phase. Runner only.

    The phase comes from the run's status, not from the caller, so a plan-phase
    runner cannot request the apply phase's unrestricted session policy.
    """
    try:
        return service.run_bundle(run_id)
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except workspace_reads.WorkspaceNotFound as error:
        raise _not_found("That run's workspace no longer exists.") from error
    except workspace_reads.ConfigVersionNotFound as error:
        raise _not_found("That run's config version no longer exists.") from error


@router.post(
    "/runs/{run_id}/artifact-uploads",
    response_model=ArtifactUpload,
    dependencies=[Depends(require_run_token())],
)
def artifact_upload(payload: ArtifactUploadCreate, run_id: str = RunId) -> dict[str, Any]:
    """Mint the presigned PUT for one of this run's artifacts. Runner only.

    The size is the caller's, not a ceiling: `Content-Length` is inside the
    signature, so S3 refuses a body of any other length. A runner therefore asks
    once per artifact, after it knows how many bytes it is about to send, and
    sends the returned headers verbatim.

    The log's key is per phase and the phase comes from the run's status, so a
    plan-phase runner cannot ask for the apply transcript's key.
    """
    try:
        return service.artifact_upload(run_id, payload.artifact, payload.size_bytes)
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.ArtifactTooLarge as error:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(error),
        ) from error


@router.post(
    "/runs/{run_id}/phase-result",
    response_model=PhaseResultAccepted,
    dependencies=[Depends(require_run_token())],
)
def phase_result(payload: PhaseResult, run_id: str = RunId) -> dict[str, Any]:
    """Record a phase's outcome and advance the run. Runner only."""
    try:
        updated = service.record_phase_result(run_id, payload.model_dump())
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.PhaseMismatch as error:
        raise _conflict("That run is not in the reported phase.") from error
    return {"run_id": run_id, "status": updated["status"]}
