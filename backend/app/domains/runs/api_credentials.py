"""A run's own control plane API token, for configurations that use the WebbPulse provider.

HCP Terraform hands a run a token scoped to it; the factory configuration that manages
this plane's workspaces needs the same, rather than a standing agent key in a workspace
variable. A workspace opts in through `run_api_token_scopes`, which only an admin who
signed in recently can set, and each bundle of its runs then carries a fresh `wpk_` key
minted under the run token tenant with the run as its subject and the workspace in its
metadata.

A speculative run, `plan_only` or a pull request's plan, gets no token at all, since its
code is whatever the branch says. A plan phase's token carries the grant's read scopes
only and is revoked when planning ends, so only the apply phase can write. The key
reaches its own workspace only (`auth.run_api_workspace_binding`), unless the workspace
holds the factory grant.

What the key may do is read live on every request (`auth.key_owner_scopes`): the
workspace's current grant, intersected with the grant it was minted under. The run
keeps only its hash, a newer bundle revokes the one before and the run's ending revokes
the last, so clearing the grant or ending the run kills the key at once.

The runner exports it as `WEBBPULSE_TF_TOKEN` beside `WEBBPULSE_TF_HOST`, the API origin.
Both environments put the API behind the access gate, so the bundle also carries the
gate's `x-origin-verify` value, read here by the runs function from its SSM parameter,
for `WEBBPULSE_TF_ORIGIN_VERIFY`. The runner task role never holds that read, since
whatever a run executes can reach the task role's credentials.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed

from ...common.composition.settings import Settings
from ...common.core.auth import (
    RUN_API_TOKEN_KIND,
    RUN_API_WRITE_SCOPES,
    RUN_TOKEN_TENANT,
    api_key_store,
    effective_run_api_scopes,
    granted_run_api_scopes,
    revoke_run_key,
)
from ...common.db import repositories

HASH_ATTRIBUTE: Final = "api_token_hash"
"""Where the run keeps the hash of its live API token."""

PHASE_TTLS: Final = {"planning": timedelta(hours=2), "applying": timedelta(hours=4)}
"""How long each phase's token lasts: the phase's own timeout, since the engine reads it once."""

SPECULATIVE_SOURCES: Final = frozenset({"vcs_pr"})
"""Run sources whose code nobody merged, which never get a token."""

_log = logging.getLogger(__name__)


def is_speculative(run: Mapping[str, Any]) -> bool:
    """Whether a run only plans code nobody merged or confirmed, such as a pull request's plan."""
    return bool(run.get("plan_only")) or str(run.get("source", "")) in SPECULATIVE_SOURCES


def phase_scopes(scopes: tuple[str, ...], status: str) -> tuple[str, ...]:
    """The part of `scopes` a token for `status` carries: all of it, less the write scopes while planning."""
    if status == "planning":
        return tuple(scope for scope in scopes if scope not in RUN_API_WRITE_SCOPES)
    return scopes


def origin_verify(settings: Settings) -> str | None:
    """The access gate's `x-origin-verify` value, or `None` with no gate or an unreadable parameter."""
    if not settings.ORIGIN_VERIFY_PARAMETER:
        return None
    import boto3

    try:
        client = boto3.client("ssm", region_name=settings.AWS_REGION_NAME or None)
        parameter = client.get_parameter(Name=settings.ORIGIN_VERIFY_PARAMETER, WithDecryption=True)
        value = str(parameter["Parameter"]["Value"])
    except Exception as error:  # noqa: BLE001
        _log.error(
            "Could not read the access gate value for a run API token.",
            extra={"event": "runs.api_token.gate_unreadable", "error": type(error).__name__},
        )
        return None
    return value or None


def issue(run: Mapping[str, Any], workspace: Mapping[str, Any], *, settings: Settings) -> dict[str, Any] | None:
    """Mint this phase's API token and swap it in for the run's previous one.

    `None` for a speculative run, when the workspace grants this phase nothing, when no
    API origin is configured, or when the run left its phase between the bundle read and the
    swap, in which case the fresh key is revoked.
    """
    from webbpulse.identity.api_keys import mint

    host = (settings.API_BASE_URL or "").rstrip("/")
    status = str(run.get("status", ""))
    if is_speculative(run):
        return None
    scopes = phase_scopes(granted_run_api_scopes(workspace), status)
    if not scopes or not host or status not in PHASE_TTLS:
        return None
    run_id = str(run["run_id"])
    workspace_id = str(run["workspace_id"])
    expires_at = datetime.now(timezone.utc) + PHASE_TTLS[status]
    minted = mint(
        user_id=run_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=phase_scopes(effective_run_api_scopes(workspace), status),
        name=f"api token {run_id}",
        expires_at=expires_at,
        store=api_key_store(settings),
        kind=RUN_API_TOKEN_KIND,
        metadata={"workspace_id": workspace_id},
    )
    try:
        old = repositories.runs(settings).update(
            {"run_id": run_id},
            update_expression=f"SET {HASH_ATTRIBUTE} = :hash",
            expression_values={":hash": minted.record.key_hash},
            condition=Attr("status").is_in(list(PHASE_TTLS)),
            return_values="UPDATED_OLD",
        )
    except ConditionFailed:
        revoke_run_key(minted.record.key_hash, settings=settings)
        return None
    previous = str((old or {}).get(HASH_ATTRIBUTE, "") or "")
    if previous and previous != minted.record.key_hash:
        revoke_run_key(previous, settings=settings)
    _log.info(
        "Issued a run's API token.",
        extra={"event": "runs.api_token.issued", "run_id": run_id, "workspace_id": workspace_id},
    )
    return {
        "host": host,
        "token": minted.plaintext,
        "expires_at": expires_at.isoformat(),
        "scopes": list(scopes),
        "origin_verify": origin_verify(settings),
    }


def revoke(run: Mapping[str, Any], *, settings: Settings) -> None:
    """Revoke the run's API token, tolerating one that is already gone."""
    revoke_run_key(str(run.get(HASH_ATTRIBUTE, "") or ""), settings=settings)


__all__ = [
    "HASH_ATTRIBUTE",
    "PHASE_TTLS",
    "SPECULATIVE_SOURCES",
    "is_speculative",
    "issue",
    "origin_verify",
    "phase_scopes",
    "revoke",
]
