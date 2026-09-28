"""The runs domain's routes, including the five the runner owns.

Routes on three different credentials. Most are guarded by `require_scopes`
and reached by a person through the JWT authorizer or an agent through a `wpk_`
key. Four, the bundle, the artifact upload, the heartbeat and the phase result, are guarded by
a run token bound to the run in the path, because the bundle carries decrypted
variables and no human scope should open it. The runner token route is guarded
by nothing but the runner task's own signed AWS identity, which is how the
runner gets that run token without it passing through the execution input.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from webbpulse.identity.claims import AuthorizerClaims

from ...common.core.auth import (
    RUNS_APPLY,
    RUNS_READ,
    RUNS_WRITE,
    RunnerRoute,
    claims,
    require_run_token,
    scopes,
    sudo,
    unauthenticated,
)
from ...common.workspaces import reads as workspace_reads
from . import phase_tasks, runner_tokens, service, vending
from .actor import actor_from_claims
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
    RunDecisionRequest,
    RunList,
    RunnerHeartbeat,
    RunnerToken,
    RunnerTokenRequest,
    RunPlan,
)

router = APIRouter()
runner_router = APIRouter(route_class=RunnerRoute)
"""The five runner routes, whose validation failures answer 401 to a caller without a run token."""

RUN_ID_PATTERN = r"^run-[0-9A-HJKMNP-TV-Z]{26}$"
RunId = Path(min_length=4, max_length=64, pattern=RUN_ID_PATTERN)
WorkspaceIdQuery = Query(
    default=None,
    min_length=4,
    max_length=64,
    pattern=r"^ws-[0-9A-HJKMNP-TV-Z]{26}$",
)


def _not_found(message: str) -> HTTPException:
    """The 404 every absent row raises."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)


def _conflict(message: str, *, error_code: str | None = None) -> HTTPException:
    """The 409 every refused transition raises, optionally carrying a stable code."""
    detail: Any = message if error_code is None else {"message": message, "error_code": error_code}
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


RUN_ROLE_MISSING_CODE = "RUN_ROLE_MISSING"
"""The code a caller matches on to send a person to the workspace's run role setup."""

PENDING_RUN_ROLE_MISSING_CODE = "PENDING_RUN_ROLE_MISSING"
"""The code a run role check carries when the workspace has no staged role to verify."""

RUN_ROLE_ASSUME_FAILED_CODE = "RUN_ROLE_ASSUME_FAILED"
"""The code a bundle carries when the workspace's run role refused the vending role.
The runner reports it as `AssumeRoleFailed`, which is what the run role check reads."""

RUN_CREDENTIALS_UNAVAILABLE_CODE = "RUN_CREDENTIALS_UNAVAILABLE"
"""The code a bundle carries when this deployment could not vend credentials at all."""

PHASE_TASK_UNRESOLVED_CODE = "PHASE_TASK_UNRESOLVED"
"""The code a phase result carries when no runner task of the phase matches the run."""

PHASE_MISMATCH_CODE = "PHASE_MISMATCH"
"""The code a heartbeat carries when the run has left the runner's phase."""

PHASE_TASK_ENDED_CODE = "PHASE_TASK_ENDED"
"""The code a heartbeat carries when the phase's state no longer waits on its task token."""


@router.post(
    "/runs",
    response_model=RunCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(RUNS_WRITE))],
)
def create_run(
    payload: RunCreate,
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Queue or start a run.

    A run queued behind the workspace's active one comes back `pending` with
    `queued_behind` set and no token, which is the caller's signal that nothing is
    executing yet.

    A workspace with no run role is a 409 carrying `RUN_ROLE_MISSING`, since the
    runner would have nothing to assume.

    `run_role_check` starts a plan only run that assumes the workspace's staged
    `pending_run_role_arn` rather than its current role. It is a 409 carrying
    `PENDING_RUN_ROLE_MISSING` when no role is staged.

    The actor is taken from the verified claims here, because this request is the
    only moment the triggering principal is known.
    """
    try:
        created = service.create_run(payload.model_dump(), actor=actor_from_claims(current))
    except workspace_reads.WorkspaceNotFound as error:
        raise _not_found("No such workspace.") from error
    except workspace_reads.RunRoleMissing as error:
        raise _conflict(
            "This workspace has no run role ARN yet, so a run has nothing to assume.",
            error_code=RUN_ROLE_MISSING_CODE,
        ) from error
    except service.PendingRunRoleMissing as error:
        raise _conflict(
            "This workspace has no staged run role, so there is nothing for a run role check to verify.",
            error_code=PENDING_RUN_ROLE_MISSING_CODE,
        ) from error
    except workspace_reads.ConfigVersionNotFound as error:
        raise _not_found("No such config version.") from error
    except service.ConfigVersionNotReady as error:
        raise _conflict("That config version has no uploaded configuration.") from error
    return service.render_run(created)


@router.get(
    "/runs",
    response_model=RunList,
    dependencies=[Depends(scopes(RUNS_READ))],
)
def list_runs(
    workspace_id: Optional[str] = WorkspaceIdQuery,
    limit: Optional[int] = Query(default=None, ge=1, le=service.MAX_RUN_PAGE_SIZE),
    cursor: Optional[str] = Query(default=None, pattern=RUN_ID_PATTERN),
) -> dict[str, Any]:
    """Runs, newest first, in one workspace or across every workspace.

    Naming `workspace_id` returns that workspace's runs in full and 404s for a
    workspace that does not exist, unchanged. Omitting it returns a page of every
    workspace's runs off the recency index; pass `next_cursor` back as `cursor`
    to continue. `limit` and `cursor` page only the cross-workspace list, so they
    are refused alongside a workspace rather than ignored.

    Both modes need exactly `runs:read`, the only check the per-workspace list
    has ever made: there is no per-workspace ACL, so the cross-workspace list
    returns nothing the caller could not list one workspace at a time.
    """
    if workspace_id is not None:
        if limit is not None or cursor is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="limit and cursor page only the cross-workspace list.",
            )
        try:
            items = service.list_runs(workspace_id)
        except workspace_reads.WorkspaceNotFound as error:
            raise _not_found("No such workspace.") from error
        return {"items": [service.render_run(item) for item in items], "next_cursor": None}

    items, next_cursor = service.list_all_runs(limit=limit or service.DEFAULT_RUN_PAGE_SIZE, cursor=cursor)
    return {"items": [service.render_run(item) for item in items], "next_cursor": next_cursor}


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
    dependencies=[Depends(sudo(RUNS_APPLY))],
)
def confirm_run(
    run_id: str = RunId,
    payload: Optional[RunDecisionRequest] = Body(default=None),
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Apply a planned run. Needs `runs:apply`, not `runs:write`, and a person's recent login.

    The body is optional; its comment is kept on the run with the confirming actor.
    """
    comment = payload.comment if payload is not None else ""
    try:
        return service.render_run(service.confirm_run(run_id, actor=actor_from_claims(current), comment=comment))
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
def discard_run(
    run_id: str = RunId,
    payload: Optional[RunDecisionRequest] = Body(default=None),
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Drop a plan that was never applied, ending its execution cleanly.

    The body is optional; its comment is kept on the run with the discarding actor.
    """
    comment = payload.comment if payload is not None else ""
    try:
        return service.render_run(service.discard_run(run_id, actor=actor_from_claims(current), comment=comment))
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
    "/runs/{run_id}/plan",
    response_model=RunPlan,
    dependencies=[Depends(scopes(RUNS_READ))],
)
def run_plan(run_id: str = RunId) -> dict[str, Any]:
    """A run's plan as structured data: the counts, the resource changes and the outputs.

    The raw `terraform show -json` document is never returned. Every value the
    plan marked sensitive is redacted before the response is built.
    """
    try:
        return service.run_plan(run_id)
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.PlanNotFound as error:
        raise _not_found("That run has no plan yet.") from error


@runner_router.get(
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
    except vending.RunRoleAssumeFailed as error:
        raise _conflict(str(error), error_code=RUN_ROLE_ASSUME_FAILED_CODE) from error
    except (vending.VendingUnavailable, vending.StateCredentialsFailed) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": str(error), "error_code": RUN_CREDENTIALS_UNAVAILABLE_CODE},
        ) from error
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except workspace_reads.WorkspaceNotFound as error:
        raise _not_found("That run's workspace no longer exists.") from error
    except workspace_reads.ConfigVersionNotFound as error:
        raise _not_found("That run's config version no longer exists.") from error


@runner_router.post(
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
    plan-phase runner cannot ask for the apply transcript's key. The applied
    outputs are an apply-phase artifact only, and a 422 anywhere else.
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
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@runner_router.post(
    "/runs/{run_id}/phase-result",
    response_model=PhaseResultAccepted,
    dependencies=[Depends(require_run_token())],
)
def phase_result(payload: PhaseResult, run_id: str = RunId) -> dict[str, Any]:
    """Record a phase's outcome, advance the run and resolve its task token. Runner only.

    The runner holds no Step Functions permission, so this is how both a result and
    a failure reach the waiting state.
    """
    try:
        updated = phase_tasks.report(run_id, payload.model_dump())
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.PhaseMismatch as error:
        raise _conflict("That run is not in the reported phase.") from error
    except phase_tasks.PhaseTaskUnresolved as error:
        raise _conflict(
            "No runner task of that phase can be matched to this run.", error_code=PHASE_TASK_UNRESOLVED_CODE
        ) from error
    return {"run_id": run_id, "status": updated["status"]}


@runner_router.post(
    "/runs/{run_id}/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_run_token())],
)
def runner_heartbeat(payload: RunnerHeartbeat, run_id: str = RunId) -> None:
    """Keep the phase's waiting state alive with a Step Functions heartbeat. Runner only.

    The runner holds no Step Functions permission, so this is how its liveness
    reaches the state's `HeartbeatSeconds`. Every 409 means the phase is over for
    this runner, which stops its engine on one.
    """
    try:
        phase_tasks.heartbeat(run_id, payload.phase)
    except service.RunNotFound as error:
        raise _not_found("No such run.") from error
    except service.PhaseMismatch as error:
        raise _conflict("That run is not in the reported phase.", error_code=PHASE_MISMATCH_CODE) from error
    except phase_tasks.PhaseTaskUnresolved as error:
        raise _conflict(
            "No runner task of that phase can be matched to this run.", error_code=PHASE_TASK_UNRESOLVED_CODE
        ) from error
    except phase_tasks.PhaseTaskEnded as error:
        raise _conflict("That phase no longer waits on its runner.", error_code=PHASE_TASK_ENDED_CODE) from error


@runner_router.post("/runs/{run_id}/runner-token", response_model=RunnerToken)
def runner_token(payload: RunnerTokenRequest, run_id: str = RunId) -> dict[str, Any]:
    """Trade a runner task's signed identity for its run token. Runner only.

    Every refusal is the same 401, a malformed request included, so a caller
    learns nothing about which check failed or what the route expects; the reason
    is logged instead.
    """
    try:
        return {"run_token": runner_tokens.exchange(run_id, payload.headers)}
    except runner_tokens.ExchangeRefused as error:
        raise unauthenticated() from error


router.include_router(runner_router)
