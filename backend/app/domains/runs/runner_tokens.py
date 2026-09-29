"""The runner token exchange: a runner task proves which task it is and gets its run's token.

A token carried on the execution input is written into the execution history,
where anyone who can read the history can take it. The runner instead signs an
STS `GetCallerIdentity` request with its task role, binding the run id into a
signed header, and sends the signed headers here. This replays the request to
STS, which answers with the caller's assumed role ARN. For an ECS task role the
session name is the task id, so the answer names both the role and the task.

The exchange then requires three things. The role is one of the runner task
roles. The task, described on the runner cluster, is still running and was
started with this run's id. And the run is in the phase that task was started
for. Only then is a token minted, the hash on the run swapped to it, and any
token the run held before revoked, so exactly one runner token is live per run.

A signed request can be replayed for as long as STS accepts it, fifteen minutes,
but only to get a token for the same run while the same task still runs, which
is what the runner already holds.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Final, Mapping

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed

from ...common.composition.settings import Settings, get_settings
from ...common.core.auth import RUN_TOKEN_TENANT, RUNNER_SCOPE, api_key_store, revoke_run_key
from ...common.db import repositories
from . import service

RUN_ID_HEADER: Final = "x-webbpulse-run-id"
"""The header that binds a signed identity request to one run."""

STS_BODY: Final = "Action=GetCallerIdentity&Version=2011-06-15"
"""The only request body the exchange forwards, whatever the caller sent."""

FORWARDED_HEADERS: Final = frozenset(
    {"authorization", "x-amz-date", "x-amz-security-token", "content-type", RUN_ID_HEADER}
)
"""The signed headers passed on to STS. Anything else the runner signed is dropped,
so its signature fails rather than smuggling a header through."""

PHASE_STATUS: Final = {"plan": "planning", "apply": "applying"}
"""The run status each phase's task is started in."""

RUNNER_TASK_ATTRIBUTE: Final = "runner_task_id"
"""Where the exchange records the task it minted for, so the phase result can
resolve that task's token without the runner calling Step Functions."""

_SIGNED_HEADERS = re.compile(r"SignedHeaders=([a-z0-9;-]+)")
_ASSUMED_ROLE = re.compile(r"^arn:aws:sts::(\d{12}):assumed-role/([\w+=,.@-]+)/([0-9a-f]{32})$")
_ARN = re.compile(r"<Arn>([^<]+)</Arn>")

_log = logging.getLogger(__name__)


class ExchangeRefused(Exception):
    """The request did not prove a runner task of this run. Always answered as a 401."""


def _sts_endpoint(settings: Settings) -> str:
    """The regional STS endpoint the runner signs for and this replays to."""
    return f"https://sts.{settings.AWS_REGION_NAME or 'us-west-2'}.amazonaws.com/"


def _forwarded(run_id: str, headers: Mapping[str, str]) -> dict[str, str]:
    """The headers to replay, after checking they bind this run and sign that binding."""
    lowered = {name.lower(): value for name, value in headers.items() if name.lower() in FORWARDED_HEADERS}
    if lowered.get(RUN_ID_HEADER) != run_id:
        raise ExchangeRefused("the signed request names another run")
    match = _SIGNED_HEADERS.search(lowered.get("authorization", ""))
    if match is None or RUN_ID_HEADER not in match.group(1).split(";"):
        raise ExchangeRefused("the run id header is not signed")
    return lowered


def caller_arn(run_id: str, headers: Mapping[str, str], *, settings: Settings) -> str:
    """Replay the signed `GetCallerIdentity` to STS and return the caller's ARN."""
    import urllib3

    forwarded = _forwarded(run_id, headers)
    response = urllib3.PoolManager(retries=False, timeout=urllib3.Timeout(total=10.0)).request(
        "POST",
        _sts_endpoint(settings),
        body=STS_BODY.encode(),
        headers=forwarded,
    )
    if response.status != 200:
        raise ExchangeRefused(f"STS answered {response.status}")
    match = _ARN.search(response.data.decode("utf-8", "replace"))
    if match is None:
        raise ExchangeRefused("STS named no caller")
    return match.group(1)


def runner_task_id(arn: str, *, settings: Settings) -> str:
    """The task id in a runner task role session ARN, refusing any other caller."""
    match = _ASSUMED_ROLE.match(arn)
    if match is None:
        raise ExchangeRefused("the caller is not an assumed role session of a task")
    account, role_name, task_id = match.groups()
    allowed = {
        (role.split(":")[4], role.rsplit("/", 1)[-1]) for role in settings.runner_task_role_arns if role.count(":") >= 5
    }
    if (account, role_name) not in allowed:
        raise ExchangeRefused("the caller is not a runner task role")
    return task_id


def _ecs(settings: Settings) -> Any:
    """An ECS client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("ecs", region_name=settings.AWS_REGION_NAME or None)


def task_environment(task_id: str, run_id: str, *, settings: Settings) -> dict[str, str]:
    """The environment overrides of a running runner task, when it was started for this run."""
    if not settings.RUNNER_CLUSTER_ARN:
        raise ExchangeRefused("no runner cluster is configured")
    described = _ecs(settings).describe_tasks(cluster=settings.RUNNER_CLUSTER_ARN, tasks=[task_id])
    tasks = described.get("tasks") or []
    if len(tasks) != 1:
        raise ExchangeRefused("the task is not on the runner cluster")
    task = tasks[0]
    if task.get("lastStatus") == "STOPPED" or task.get("desiredStatus") == "STOPPED":
        raise ExchangeRefused("the task has stopped")
    environment: dict[str, str] = {}
    for override in (task.get("overrides") or {}).get("containerOverrides") or []:
        for entry in override.get("environment") or []:
            environment[str(entry.get("name", ""))] = str(entry.get("value", ""))
    if environment.get("RUN_ID") != run_id:
        raise ExchangeRefused("the task was started for another run")
    return environment


def task_phase(task_id: str, run_id: str, *, settings: Settings) -> str:
    """The phase a running runner task was started for, when it was started for this run."""
    phase = task_environment(task_id, run_id, settings=settings).get("PHASE", "")
    if phase not in PHASE_STATUS:
        raise ExchangeRefused("the task names no phase")
    return phase


def phase_task_token(run: Mapping[str, Any], phase: str, *, settings: Settings) -> str:
    """The Step Functions task token of the runner task that exchanged for this run's token.

    Read from the task's own overrides through `DescribeTasks`, never from the
    caller, so a runner can only ever resolve the token its own phase waits on.

    Raises:
        ExchangeRefused: No task exchanged for this run, or it no longer matches.
    """
    task_id = str(run.get(RUNNER_TASK_ATTRIBUTE, "") or "")
    if not task_id:
        raise ExchangeRefused("no runner task has exchanged for this run")
    environment = task_environment(task_id, str(run["run_id"]), settings=settings)
    if environment.get("PHASE") != phase:
        raise ExchangeRefused("the task was started for another phase")
    token = environment.get("TASK_TOKEN", "")
    if not token:
        raise ExchangeRefused("the task carries no task token")
    return token


def exchange(run_id: str, headers: Mapping[str, str], *, settings: Settings | None = None) -> str:
    """Mint the run's runner token for the task that signed `headers`, returning the plaintext.

    A refusal is logged with its reason here, since the caller only ever sees a 401.
    """
    try:
        return _exchange(run_id, headers, settings=settings or get_settings())
    except ExchangeRefused as error:
        _log.warning(
            "A runner token exchange was refused.",
            extra={"event": "runs.runner_token.refused", "run_id": run_id, "reason": str(error)},
        )
        raise


def _exchange(run_id: str, headers: Mapping[str, str], *, settings: Settings) -> str:
    """Check the signed identity against the task and the run, then swap in a fresh token.

    The hash swap is conditional on the run still being in the task's phase, so a
    run that ended between the checks and the write gets no live token, and the
    hash it replaced is read from the write itself, so two exchanges racing each
    other still revoke every token but the last.
    """
    from webbpulse.identity.api_keys import mint

    resolved = settings
    task_id = runner_task_id(caller_arn(run_id, headers, settings=resolved), settings=resolved)
    phase = task_phase(task_id, run_id, settings=resolved)
    try:
        run = service.get_run(run_id, settings=resolved)
    except service.RunNotFound as error:
        raise ExchangeRefused("no such run") from error
    expected = PHASE_STATUS[phase]
    if run.get("status") != expected:
        raise ExchangeRefused("the run is not in the task's phase")

    store = api_key_store(resolved)
    minted = mint(
        user_id=run_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=(RUNNER_SCOPE,),
        name=f"run token {run_id} {phase}",
        expires_at=datetime.now(timezone.utc) + service.RUN_TOKEN_TTL,
        store=store,
    )
    try:
        old = repositories.runs(resolved).update(
            {"run_id": run_id},
            update_expression=f"SET run_token_hash = :hash, {RUNNER_TASK_ATTRIBUTE} = :task",
            expression_values={":hash": minted.record.key_hash, ":task": task_id},
            condition=Attr("status").eq(expected),
            return_values="UPDATED_OLD",
        )
    except ConditionFailed as error:
        revoke_run_key(minted.record.key_hash, settings=resolved)
        raise ExchangeRefused("the run left the task's phase") from error
    previous = str((old or {}).get("run_token_hash", "") or "")
    if previous and previous != minted.record.key_hash:
        revoke_run_key(previous, settings=resolved)
    _log.info(
        "The runner exchanged its task identity for a run token.",
        extra={"event": "runs.runner_token.exchanged", "run_id": run_id, "phase": phase, "task_id": task_id},
    )
    return minted.plaintext


__all__ = [
    "ExchangeRefused",
    "FORWARDED_HEADERS",
    "PHASE_STATUS",
    "RUNNER_TASK_ATTRIBUTE",
    "RUN_ID_HEADER",
    "STS_BODY",
    "caller_arn",
    "exchange",
    "phase_task_token",
    "runner_task_id",
    "task_environment",
    "task_phase",
]
