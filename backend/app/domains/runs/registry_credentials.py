"""A run's module registry credential, like the `TF_TOKEN` HCP injects for its own host.

Each bundle carries a fresh `wpk_` key scoped `runner:registry`, minted under the run
token tenant with the run as its subject, so it reads the private registry and
nothing else. It lasts an hour, the run keeps only its hash, a newer bundle revokes
the one before and the run's ending revokes the last, so no registry key outlives
its phase by more than the time it takes to end the run.

It travels only in the bundle, which the runner fetches with its run token, never
in the Step Functions input or the task overrides. The runner sets it as
`TF_TOKEN_<host>` for `init` alone, for the host module sources name, which is the
SPA host that serves `/.well-known/terraform.json`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping
from urllib.parse import urlparse

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed

from ...common.composition.settings import Settings
from ...common.core.auth import RUN_TOKEN_TENANT, RUNNER_REGISTRY_SCOPE, api_key_store, revoke_run_key
from ...common.db import repositories

REGISTRY_TOKEN_TTL: Final = timedelta(hours=1)
"""Long enough for `init` at the start of a phase, which is the only time it is set."""

HASH_ATTRIBUTE: Final = "registry_token_hash"
"""Where the run keeps the hash of its live registry credential."""

PHASE_STATUSES: Final = ("planning", "applying")
"""The statuses a runner phase fetches its bundle in."""

_log = logging.getLogger(__name__)


def registry_hosts(settings: Settings) -> list[str]:
    """The hosts module sources name for this registry: the SPA host, when configured."""
    host = urlparse(settings.IDENTITY_FRONTEND_BASE_URL or "").hostname or ""
    return [host] if host else []


def issue(run: Mapping[str, Any], *, settings: Settings) -> dict[str, Any] | None:
    """Mint this phase's registry credential and swap it in for the run's previous one.

    `None` when no registry host is configured, or when the run left its phase
    between the bundle read and the swap, in which case the fresh key is revoked.
    """
    from webbpulse.identity.api_keys import mint

    hosts = registry_hosts(settings)
    if not hosts:
        return None
    run_id = str(run["run_id"])
    store = api_key_store(settings)
    expires_at = datetime.now(timezone.utc) + REGISTRY_TOKEN_TTL
    minted = mint(
        user_id=run_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=(RUNNER_REGISTRY_SCOPE,),
        name=f"registry token {run_id}",
        expires_at=expires_at,
        store=store,
    )
    try:
        old = repositories.runs(settings).update(
            {"run_id": run_id},
            update_expression=f"SET {HASH_ATTRIBUTE} = :hash",
            expression_values={":hash": minted.record.key_hash},
            condition=Attr("status").is_in(list(PHASE_STATUSES)),
            return_values="UPDATED_OLD",
        )
    except ConditionFailed:
        revoke_run_key(minted.record.key_hash, settings=settings)
        return None
    previous = str((old or {}).get(HASH_ATTRIBUTE, "") or "")
    if previous and previous != minted.record.key_hash:
        revoke_run_key(previous, settings=settings)
    _log.info(
        "Issued a run's registry credential.",
        extra={"event": "runs.registry_token.issued", "run_id": run_id},
    )
    return {"hosts": hosts, "token": minted.plaintext, "expires_at": expires_at.isoformat()}


def revoke(run: Mapping[str, Any], *, settings: Settings) -> None:
    """Revoke the run's registry credential, tolerating one that is already gone."""
    revoke_run_key(str(run.get(HASH_ATTRIBUTE, "") or ""), settings=settings)


__all__ = ["HASH_ATTRIBUTE", "REGISTRY_TOKEN_TTL", "issue", "registry_hosts", "revoke"]
