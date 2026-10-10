"""The runs function's share of `tfe.v2`: runs, their plans and applies, and the logs the CLI streams.

These are the calls `terraform plan` and `apply` make through a `cloud {}` block once
the configuration is uploaded: create a run, poll it and its workspace's queue, stream
the plan's log, then confirm or discard it and stream the apply's. Every route answers
in HCP's JSON:API shapes and reuses the runs service, so a CLI run is the same run the
console and the v1 API see.

Served at `/api/v2` behind no authorizer, like the workspaces function's share, because
the CLI sends the `terraform login` key as a bearer token. The two log routes take no
bearer at all: go-tfe fetches a log URL bare, so its credential is the HMAC in the path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from webbpulse.identity.scopes import claims_scopes

from ...common import audit
from ...common.composition.settings import get_settings
from ...common.core import variable_cipher
from ...common.core.auth import RUNS_APPLY, RUNS_READ, RUNS_WRITE, scopes
from ...common.tfe.jsonapi import (
    JsonApiRoute,
    conflict,
    document,
    not_found,
    paginate,
    related_id,
    request_attributes,
    resource,
    unprocessable,
)
from ...common.tfe.resources import require_organization, workspace_resource
from ...common.workspaces import reads as workspace_reads
from . import run_options, service, tfe_runs
from .actor import actor_from_claims
from .schemas.run import Phase

if TYPE_CHECKING:  # pragma: no cover
    from webbpulse.identity.claims import AuthorizerClaims

router = APIRouter(prefix="/api/v2", include_in_schema=False, route_class=JsonApiRoute)

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"

RunId = Path(min_length=4, max_length=64, pattern=rf"^run-{ULID}$")

PlanId = Path(min_length=5, max_length=64, pattern=rf"^plan-{ULID}$")

ApplyId = Path(min_length=6, max_length=64, pattern=rf"^apply-{ULID}$")

WorkspaceId = Path(min_length=4, max_length=64, pattern=rf"^ws-{ULID}$")

LogToken = Path(min_length=3, max_length=128, pattern=r"^[0-9]+\.[A-Za-z0-9_-]+$")

MAX_LOG_CHUNK = 1024 * 1024
"""The most bytes one log read returns, well past go-tfe's 64 KiB reads."""


def _run_or_404(run_id: str) -> dict[str, Any]:
    """One run, or the 404 go-tfe maps to `ErrResourceNotFound`."""
    try:
        return service.get_run(run_id)
    except service.RunNotFound as error:
        raise not_found("run") from error


def _base_url(request: Request) -> str:
    """The public origin log URLs are built on, the configured API host where there is one."""
    return (get_settings().API_BASE_URL or str(request.base_url)).rstrip("/")


def _log_url(request: Request, kind: str, phase_identifier: str) -> str:
    """A signed log URL for one plan or apply, readable without a bearer for a whole phase."""
    try:
        token = tfe_runs.log_token(phase_identifier, get_settings())
    except variable_cipher.MasterKeyUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail={"message": "Run logs cannot be signed in this environment.", "error_code": "UNAVAILABLE"},
        ) from error
    return f"{_base_url(request)}/api/v2/{kind}/{phase_identifier}/logs/{token}"


def _plan(run: Mapping[str, Any], request: Request) -> dict[str, Any]:
    """The run's plan resource with its signed log URL."""
    return tfe_runs.plan_resource(run, _log_url(request, "plans", tfe_runs.plan_id(str(run["run_id"]))))


def _apply(run: Mapping[str, Any], request: Request) -> dict[str, Any]:
    """The run's apply resource with its signed log URL."""
    return tfe_runs.apply_resource(run, _log_url(request, "applies", tfe_runs.apply_id(str(run["run_id"]))))


def _included(run: Mapping[str, Any], request: Request, held: Any) -> list[dict[str, Any]]:
    """The resources a run read's `include` asks for. Unknown names are ignored, as HCP does."""
    wanted = {name.strip() for name in request.query_params.get("include", "").split(",") if name.strip()}
    included: list[dict[str, Any]] = []
    if "workspace" in wanted:
        try:
            workspace = workspace_reads.get_workspace(str(run["workspace_id"]))
        except workspace_reads.WorkspaceNotFound:
            workspace = None
        if workspace is not None:
            included.append(workspace_resource(workspace, held))
    if "plan" in wanted:
        included.append(_plan(run, request))
    if "apply" in wanted:
        included.append(_apply(run, request))
    return included


def _variables(value: Any) -> dict[str, str] | None:
    """A run's `variables` list as the key and HCL value map the service takes."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise unprocessable("variables must be a list of key and value objects.")
    variables: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("key"), str):
            raise unprocessable("Each run variable needs a string key and value.")
        variables[str(entry["key"])] = str(entry.get("value", ""))
    return variables


def _create_payload(
    attributes: Mapping[str, Any], workspace_id: str, config_version_id: str, speculative: bool
) -> dict[str, Any]:
    """The runs service's create payload for one `POST /runs` body."""
    payload: dict[str, Any] = {
        "workspace_id": workspace_id,
        "config_version_id": config_version_id,
        "plan_only": bool(attributes.get("plan-only") or speculative),
        "is_destroy": bool(attributes.get("is-destroy", False)),
        "message": str(attributes.get("message") or ""),
        "target_addrs": attributes.get("target-addrs"),
        "replace_addrs": attributes.get("replace-addrs"),
        "refresh": attributes.get("refresh", True),
        "refresh_only": attributes.get("refresh-only", False),
        "run_variables": _variables(attributes.get("variables")),
    }
    if isinstance(attributes.get("auto-apply"), bool):
        payload["auto_apply"] = attributes["auto-apply"]
    if attributes.get("save-plan") is True and not payload["plan_only"]:
        payload["save_plan"] = True
    return payload


@router.post("/runs", status_code=201)
async def create_run(
    request: Request,
    current: "AuthorizerClaims" = Depends(scopes(RUNS_WRITE)),
) -> Response:
    """Queue or start a run on an uploaded configuration version.

    A run on a speculative configuration version is plan only, which is how a
    `terraform plan` without `-out` asks for one. `auto-apply`, which the CLI sends
    for `-auto-approve`, needs `runs:apply`, since it is a confirmation made up front.
    `save-plan`, which `terraform plan -out` sends, keeps the plan waiting for the
    `terraform apply <planfile>` that confirms it, and never applies on its own.
    """
    attributes, relationships = await request_attributes(request)
    held = claims_scopes(current)
    workspace_id = related_id(relationships, "workspace")
    config_version_id = related_id(relationships, "configuration-version")
    if not workspace_id or not config_version_id:
        raise unprocessable("A run needs a workspace and a configuration version.")
    if attributes.get("auto-apply") is True and RUNS_APPLY not in held:
        raise unprocessable("Auto-approving a run needs the runs:apply scope.")
    try:
        config_version = workspace_reads.get_config_version(workspace_id, config_version_id)
        payload = _create_payload(attributes, workspace_id, config_version_id, bool(config_version.get("speculative")))
        created = service.create_run(payload, actor=actor_from_claims(current))
    except workspace_reads.WorkspaceNotFound as error:
        raise not_found("workspace") from error
    except workspace_reads.ConfigVersionNotFound as error:
        raise not_found("configuration version") from error
    except service.ConfigVersionNotReady as error:
        raise unprocessable("That configuration version has no uploaded configuration.") from error
    except workspace_reads.RunRoleMissing as error:
        raise conflict("This workspace has no run role yet, so a run has nothing to assume.") from error
    except run_options.InvalidRunOptions as error:
        raise unprocessable(str(error)) from error
    except variable_cipher.MasterKeyUnavailable as error:
        raise unprocessable("Run variables cannot be sealed in this environment.") from error
    return document(tfe_runs.run_resource(created, held), status_code=201)


@router.get("/runs/{run_id}")
def read_run(
    request: Request,
    run_id: str = RunId,
    current: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """One run, with the workspace, plan and apply it names when `include` asks for them."""
    held = claims_scopes(current)
    run = _run_or_404(run_id)
    return document(tfe_runs.run_resource(run, held), included=_included(run, request, held))


@router.get("/workspaces/{workspace_id}/runs")
def list_workspace_runs(
    request: Request,
    workspace_id: str = WorkspaceId,
    current: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """A workspace's runs, newest first, which the CLI walks to place its run in the queue."""
    held = claims_scopes(current)
    try:
        runs = service.list_runs(workspace_id)
    except workspace_reads.WorkspaceNotFound as error:
        raise not_found("workspace") from error
    page, meta = paginate(runs, request)
    return document([tfe_runs.run_resource(run, held) for run in page], meta=meta)


@router.get("/runs/{run_id}/run-events")
def list_run_events(
    request: Request,
    run_id: str = RunId,
    _: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """A run's events, which are empty: the run's timeline lives on the run itself."""
    _run_or_404(run_id)
    page, meta = paginate([], request)
    return document(page, meta=meta)


@router.post("/runs/{run_id}/actions/apply", status_code=202)
def apply_run(
    request: Request,
    run_id: str = RunId,
    current: "AuthorizerClaims" = Depends(scopes(RUNS_APPLY)),
) -> Response:
    """Confirm a planned run, which is what answering `yes` at the CLI's prompt sends."""
    try:
        confirmed = service.confirm_run(run_id, actor=actor_from_claims(current))
    except service.RunNotFound as error:
        raise not_found("run") from error
    except service.RunNotConfirmable as error:
        raise conflict("This run is not awaiting a confirmation.") from error
    audit.record_run_decision(audit.RUN_CONFIRMED, request=request, claims=current, run=confirmed)
    return Response(status_code=202)


@router.post("/runs/{run_id}/actions/discard", status_code=202)
def discard_run(
    request: Request,
    run_id: str = RunId,
    current: "AuthorizerClaims" = Depends(scopes(RUNS_WRITE)),
) -> Response:
    """Discard a planned run, which is what answering anything else at the prompt sends."""
    try:
        discarded = service.discard_run(run_id, actor=actor_from_claims(current))
    except service.RunNotFound as error:
        raise not_found("run") from error
    except service.RunNotDiscardable as error:
        raise conflict("This run has no plan awaiting a decision.") from error
    audit.record_run_decision(audit.RUN_DISCARDED, request=request, claims=current, run=discarded)
    return Response(status_code=202)


@router.post("/runs/{run_id}/actions/cancel", status_code=202)
def cancel_run(
    run_id: str = RunId,
    _: "AuthorizerClaims" = Depends(scopes(RUNS_WRITE)),
) -> Response:
    """Cancel a run, which is what an interrupt at the CLI sends."""
    try:
        service.cancel_run(run_id)
    except service.RunNotFound as error:
        raise not_found("run") from error
    except service.RunNotCancellable as error:
        raise conflict("This run can no longer be cancelled.") from error
    return Response(status_code=202)


@router.get("/plans/{plan_id}")
def read_plan(
    request: Request,
    plan_id: str = PlanId,
    _: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """A run's plan, which go-tfe reads for its log URL and again to know the log is done."""
    return document(_plan(_run_or_404(tfe_runs.run_id_of(plan_id)), request))


@router.get("/applies/{apply_id}")
def read_apply(
    request: Request,
    apply_id: str = ApplyId,
    _: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """A run's apply, read like its plan."""
    return document(_apply(_run_or_404(tfe_runs.run_id_of(apply_id)), request))


def _log(phase_identifier: str, token: str, phase: Phase, offset: int, limit: int) -> Response:
    """One window of a phase's log, for a path token signed for that phase."""
    settings = get_settings()
    if not tfe_runs.verify_log_token(phase_identifier, token, settings):
        raise not_found("log")
    run = _run_or_404(tfe_runs.run_id_of(phase_identifier))
    body = tfe_runs.log_window(run, phase, offset, limit, settings)
    return Response(content=body, media_type="text/plain", headers={"Cache-Control": "no-store"})


@router.get("/plans/{plan_id}/logs/{token}")
def read_plan_log(
    plan_id: str = PlanId,
    token: str = LogToken,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=MAX_LOG_CHUNK, ge=0),
) -> Response:
    """The plan's log from `offset`, framed for go-tfe's log reader."""
    return _log(plan_id, token, "plan", offset, min(limit, MAX_LOG_CHUNK))


@router.get("/applies/{apply_id}/logs/{token}")
def read_apply_log(
    apply_id: str = ApplyId,
    token: str = LogToken,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=MAX_LOG_CHUNK, ge=0),
) -> Response:
    """The apply's log from `offset`, framed for go-tfe's log reader."""
    return _log(apply_id, token, "apply", offset, min(limit, MAX_LOG_CHUNK))


@router.get("/organizations/{organization}/runs/queue")
def read_run_queue(
    organization: str,
    request: Request,
    _: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """The organization's run queue, empty: runs queue per workspace, never on shared capacity."""
    require_organization(organization)
    page, meta = paginate([], request)
    return document(page, meta=meta)


@router.get("/organizations/{organization}/capacity")
def read_capacity(
    organization: str,
    _: "AuthorizerClaims" = Depends(scopes(RUNS_READ)),
) -> Response:
    """The organization's run capacity, which has no organization wide limit to report."""
    require_organization(organization)
    return document(resource("organization-capacity", organization, {"pending": 0, "running": 0}))
