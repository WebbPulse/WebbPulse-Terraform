"""A run's own control plane API token, for configurations that use the WebbPulse provider.

HCP Terraform hands a run a token scoped to it; the factory configuration that manages
this plane's workspaces needs the same, rather than a standing agent key in a workspace
variable. A workspace opts in through `run_api_token_scopes`, which only an admin who
signed in recently can set, and each bundle of its runs then carries a fresh `wpk_` key
minted under the run token tenant with the run as its subject and the workspace in its
metadata.

A plan phase's token carries the grant's read scopes only and is revoked when planning
ends, so only the apply phase can write. A speculative run, `plan_only` or a pull
request's plan, runs code nobody merged, so like HCP's speculative plans it gets that
read-only plan token in every phase and never a write scope, which lets a pull request
plan refresh what the provider manages. The key reaches its own workspace only
(`auth.run_api_workspace_binding`), unless the workspace holds the factory grant, which
without a write scope adds only the reach to read every workspace.

A grant set before the factory grant existed reached every workspace through its write
scopes alone, so the first token minted for such a grant (`auth.RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE`
absent, every write scope held) adds `workspaces:factory` and stamps the version, once,
so the factory workspace keeps working through the release without an admin's step-up.

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
    RUN_API_TOKEN_SCOPES_ATTRIBUTE,
    RUN_API_TOKEN_SCOPES_VERSION,
    RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE,
    RUN_API_WRITE_SCOPES,
    WORKSPACES_FACTORY,
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
"""Run sources whose code nobody merged, whose token is always the read-only plan token."""

_log = logging.getLogger(__name__)


def is_speculative(run: Mapping[str, Any]) -> bool:
    """Whether a run only plans code nobody merged or confirmed, such as a pull request's plan."""
    return bool(run.get("plan_only")) or str(run.get("source", "")) in SPECULATIVE_SOURCES


def phase_scopes(scopes: tuple[str, ...], status: str) -> tuple[str, ...]:
    """The part of `scopes` a token for `status` carries: all of it, less the write scopes while planning."""
    if status == "planning":
        return tuple(scope for scope in scopes if scope not in RUN_API_WRITE_SCOPES)
    return scopes


def migrate_factory_grant(workspace: Mapping[str, Any], *, settings: Settings) -> Mapping[str, Any]:
    """The workspace with a grant set before the factory grant existed given `workspaces:factory`.

    Such a grant (no version stamp) holding every write scope is what the factory workspace
    carried when write scopes alone reached every workspace, so it keeps that reach. The
    write is conditional on the stamp still being absent, so a concurrent admin edit, which
    stamps it, wins, and a repeat is a no-op. Any other workspace comes back unchanged.
    """
    if workspace.get(RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE) is not None:
        return workspace
    granted = granted_run_api_scopes(workspace)
    if WORKSPACES_FACTORY in granted or not RUN_API_WRITE_SCOPES <= set(granted):
        return workspace
    workspace_id = str(workspace["workspace_id"])
    try:
        updated = repositories.workspaces(settings).update(
            {"workspace_id": workspace_id},
            update_expression="SET #scopes = list_append(#scopes, :factory), #version = :version",
            expression_names={
                "#scopes": RUN_API_TOKEN_SCOPES_ATTRIBUTE,
                "#version": RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE,
            },
            expression_values={":factory": [WORKSPACES_FACTORY], ":version": RUN_API_TOKEN_SCOPES_VERSION},
            condition=Attr("workspace_id").exists() & Attr(RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE).not_exists(),
            return_values="ALL_NEW",
        )
    except ConditionFailed:
        return repositories.workspaces(settings).get({"workspace_id": workspace_id}, consistent=True) or workspace
    _log.info(
        "Gave a pre-factory run API token grant the factory grant.",
        extra={"event": "runs.api_token.factory_migrated", "workspace_id": workspace_id},
    )
    return updated or workspace


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

    A speculative run always gets the plan phase's read-only token and lifetime. `None`
    when the workspace grants this phase nothing, when no API origin is configured, or when
    the run left its phase between the bundle read and the swap, in which case the fresh
    key is revoked.
    """
    from webbpulse.identity.api_keys import mint

    host = (settings.API_BASE_URL or "").rstrip("/")
    status = str(run.get("status", ""))
    if not host or status not in PHASE_TTLS:
        return None
    workspace = migrate_factory_grant(workspace, settings=settings)
    phase = "planning" if is_speculative(run) else status
    scopes = phase_scopes(granted_run_api_scopes(workspace), phase)
    if not scopes:
        return None
    run_id = str(run["run_id"])
    workspace_id = str(run["workspace_id"])
    expires_at = datetime.now(timezone.utc) + PHASE_TTLS[phase]
    minted = mint(
        user_id=run_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=phase_scopes(effective_run_api_scopes(workspace), phase),
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
    "migrate_factory_grant",
    "origin_verify",
    "phase_scopes",
    "revoke",
]
