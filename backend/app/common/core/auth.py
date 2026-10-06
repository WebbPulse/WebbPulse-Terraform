"""Authorization for every route: human JWT claims, agent API keys, run tokens.

Two credentials reach the human and agent routes and one reaches the runner's.

A person arrives with a JWT the API Gateway authorizer already verified, so
`webbpulse.identity.claims` reads the claims rather than checking a signature. An
agent arrives with a `wpk_` key, which `claims_or_api_key` verifies against the
identity `api-keys` table and renders as the same claims object, so a route
guarded by `require_scopes` cannot tell the two apart.

A person may also arrive with a `wp-tf login` device token, which is accepted only
while its grant is live, checked through `device_grant_liveness`.

The runner arrives with a run token, which is a `wpk_` key the runs domain mints
per run with the `runner` scope and an expiry. It is not interchangeable with an
agent key: `require_run_token` additionally checks the key is bound to the run in
the path, so a token for one run cannot read another run's bundle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Final

import anyio.from_thread
from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import Response
from webbpulse.identity import DeviceGrantLiveness, dynamo_device_grant_stores
from webbpulse.identity.api_keys import ApiKeyRecord, ApiKeyStore, DynamoApiKeyStore, verify
from webbpulse.identity.claims import AuthorizerClaims
from webbpulse.identity.scopes import bearer_credential, claims_or_api_key, require_recent_auth, require_scopes

from ..composition.settings import Settings, get_settings
from ..db.identity_tables import identity_table_prefix

WORKSPACES_READ: Final = "workspaces:read"
WORKSPACES_WRITE: Final = "workspaces:write"
VARIABLES_READ: Final = "variables:read"
VARIABLES_WRITE: Final = "variables:write"
CONFIGS_READ: Final = "configs:read"
CONFIGS_WRITE: Final = "configs:write"
RUNS_READ: Final = "runs:read"
RUNS_WRITE: Final = "runs:write"
RUNS_APPLY: Final = "runs:apply"
STATE_DOWNLOAD: Final = "state:download"
"""Explicit access to raw state, excluded from ordinary read-only grants."""
REGISTRY_READ: Final = "registry:read"
"""Reading the module registry, which is what `TF_TOKEN_<host>` carries for `terraform init`."""
REGISTRY_WRITE: Final = "registry:write"
"""Connecting registry modules to repositories and deleting them."""
ADMIN: Final = "admin"
"""Operator settings such as the GitHub App. It does not end in `:read`, so only an
admin holds it, and a key carries it only when an admin minted it."""

RUNNER_SCOPE: Final = "runner"
"""The scope a run token carries. Never granted to a human or an agent key: it
reaches only the runner routes, and only for the run it is bound to."""

RUNNER_REGISTRY_SCOPE: Final = "runner:registry"
"""The scope of a run's registry credential, which the runner sets as `TF_TOKEN_<host>`
for `terraform init` alone. It opens the module registry protocol and nothing else:
the key's subject is a run, which holds no user scopes, so every other route refuses it."""

ALL_SCOPES: Final = (
    WORKSPACES_READ,
    WORKSPACES_WRITE,
    VARIABLES_READ,
    VARIABLES_WRITE,
    CONFIGS_READ,
    CONFIGS_WRITE,
    RUNS_READ,
    RUNS_WRITE,
    RUNS_APPLY,
    STATE_DOWNLOAD,
    REGISTRY_READ,
    REGISTRY_WRITE,
    ADMIN,
)
"""Every scope a human or an agent can hold, which is what the contract lists."""

RUN_TOKEN_TENANT: Final = "webbpulse-terraform"
"""The tenant every run token is minted under. The control plane is single tenant,
and the claim is required, so one constant stands in for it."""

RUN_KEY_USER_PREFIX: Final = "run-"
"""The subject prefix of every run-scoped key: runner tokens and registry credentials."""

RUN_KEY_TTL_ATTRIBUTE: Final = "purge_at"
"""The api-keys table's TTL attribute, in epoch seconds, stamped only on revoked run keys."""

RUN_KEY_RETENTION: Final = timedelta(days=1)
"""How long a revoked run key stays readable before DynamoDB TTL removes it."""


def api_key_store(settings: Settings | None = None) -> ApiKeyStore:
    """The identity `api-keys` table, which holds agent keys and run tokens alike.

    The table is the identity module's, named by the package's own constant, so a
    rename there is a failing import here rather than a silent miss.

    The prefix comes from `identity_table_prefix`, which reads the one Terraform
    sets. Deriving it from `ENVIRONMENT` would name the wrong table in production,
    where the stack slugs the environment to `prod`.
    """
    return DynamoApiKeyStore(_api_keys_repository(settings))


def _api_keys_repository(settings: Settings | None = None) -> Any:
    """The `webbpulse.dynamodb.Repository` over the identity `api-keys` table."""
    from webbpulse.dynamodb import Repository
    from webbpulse.identity.api_keys import API_KEYS_TABLE

    resolved = settings or get_settings()
    return Repository(
        API_KEYS_TABLE,
        prefix=identity_table_prefix(resolved),
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.dynamodb_endpoint_url,
    )


def revoke_run_key(key_hash: str, *, settings: Settings | None = None, now: datetime | None = None) -> None:
    """Revoke a run-scoped key and stamp the TTL that removes its row `RUN_KEY_RETENTION` later.

    The stamp is conditional on the row existing and belonging to a run, so it never
    resurrects a deleted row or schedules an agent key for deletion. A key that was
    already revoked still gets the stamp, which heals rows revoked before it existed.
    """
    from boto3.dynamodb.conditions import Attr
    from webbpulse.dynamodb import ConditionFailed

    if not key_hash:
        return
    repository = _api_keys_repository(settings)
    DynamoApiKeyStore(repository).revoke(key_hash)
    purge_at = int(((now or datetime.now(timezone.utc)) + RUN_KEY_RETENTION).timestamp())
    try:
        repository.update(
            {"key_hash": key_hash},
            update_expression="SET #purge = :purge",
            expression_names={"#purge": RUN_KEY_TTL_ATTRIBUTE},
            expression_values={":purge": purge_at},
            condition=Attr("key_hash").exists() & Attr("user_id").begins_with(RUN_KEY_USER_PREFIX),
        )
    except ConditionFailed:
        return


def key_owner_scopes(record: ApiKeyRecord) -> tuple[str, ...]:
    """The scopes a key's owner holds right now, read from the `users` table.

    Nothing for an owner that is gone, disabled or unverified, which is the same
    test `may_authenticate` applies at sign-in. Otherwise the scopes the owner's
    current role earns, the same ones a fresh session token would carry.
    """
    from ..db.users import UserRepository
    from ..identity.identity_hooks import ADMIN_ROLE, scope_claim_for_roles

    user = UserRepository().get(record.user_id)
    if user is None or user.disabled or not user.email_verified:
        return ()
    return tuple(scope_claim_for_roles([ADMIN_ROLE] if user.is_admin else []).split())


_DEVICE_LIVENESS: dict[str, DeviceGrantLiveness] = {}


def device_grant_liveness(settings: Settings | None = None) -> DeviceGrantLiveness | None:
    """The cached device grant liveness check, or None while device login is off.

    One instance per table prefix lives for the process, so its few seconds of
    cache span requests. None makes `claims_or_api_key` refuse every device token,
    which is the right answer where the device grant is not enabled.
    """
    resolved = settings or get_settings()
    if not resolved.IDENTITY_DEVICE_GRANT_ENABLED:
        return None
    prefix = identity_table_prefix(resolved)
    liveness = _DEVICE_LIVENESS.get(prefix)
    if liveness is None:
        stores = dynamo_device_grant_stores(
            prefix,
            region_name=resolved.AWS_REGION_NAME or None,
            endpoint_url=resolved.dynamodb_endpoint_url,
        )
        liveness = _DEVICE_LIVENESS.setdefault(prefix, DeviceGrantLiveness(stores.grants))
    return liveness


def reset_device_grant_liveness() -> None:
    """Drop the cached liveness checks, for a suite that swaps the tables underneath them."""
    _DEVICE_LIVENESS.clear()


def _claims_dependency() -> Any:
    """The claims dependency accepting a verified JWT or a `wpk_` agent key.

    The store is resolved per request rather than captured, so a test that moves
    the table underneath the settings is read rather than a stale one. A key's
    stored scopes are intersected with `key_owner_scopes`, so a key loses what its
    owner loses: a demoted admin's keys fall to the read scopes and a disabled or
    deleted owner's keys hold nothing.
    """

    async def dependency(request: Request) -> AuthorizerClaims:
        """Return this request's verified claims, or raise a 401."""
        inner = claims_or_api_key(
            store=api_key_store(), live_scopes=key_owner_scopes, device_grants=device_grant_liveness()
        )
        result: AuthorizerClaims = await inner(request)
        return result

    dependency.__name__ = "claims_or_api_key"
    dependency.__doc__ = "The verified claims for this request, from the authorizer or an API key."
    return dependency


claims = _claims_dependency()
"""The dependency every guarded route resolves its claims through."""


def scopes(*required: str) -> Any:
    """A dependency refusing any caller missing one of `required`.

    Thin wrapper over `require_scopes`, bound to this project's claims
    dependency, so no route has to remember to pass it.
    """
    return require_scopes(*required, claims_dependency=claims)


STEP_UP_MAX_AGE_SECONDS: Final = 15 * 60
"""How recent a person's login must be for a sensitive change, like HCP and GitHub sudo mode.

A person whose login is older gets a 401 `STEP_UP_REQUIRED` and confirms their password
through `/api/auth/step-up`. An agent key has no login to age and passes; its scopes are
what limit it."""

recent_auth = require_recent_auth(STEP_UP_MAX_AGE_SECONDS, claims_dependency=claims)
"""The step-up gate on its own, for a route that already checks its caller another way."""


def sudo(*required: str) -> Any:
    """A dependency requiring `required` and then a login within `STEP_UP_MAX_AGE_SECONDS`.

    The scope check runs first, so a caller who could never make the change gets a 403
    rather than a password prompt that leads nowhere.
    """
    return require_recent_auth(STEP_UP_MAX_AGE_SECONDS, claims_dependency=scopes(*required))


def ensure_recent_auth(current: AuthorizerClaims) -> None:
    """Apply the step-up gate from inside a sync route, for a change only sometimes sensitive.

    Such as a PATCH that happens to change the run role: the route decides from the body
    and the stored row, then raises the same 401 `recent_auth` would.
    """
    anyio.from_thread.run(_recent_auth_check, current)


async def _recent_auth_check(current: AuthorizerClaims) -> None:
    """Run the package gate against claims this request already resolved."""
    await recent_auth(current)


def unauthenticated() -> HTTPException:
    """The 401 every failed run token path raises, with no detail of which check failed."""
    return HTTPException(
        status_code=401,
        detail={"message": "Authentication is required.", "error_code": "UNAUTHORIZED"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def run_token_record(request: Request, run_id: str) -> ApiKeyRecord:
    """The verified run token for `run_id`, or a 401.

    Three checks, all failing the same way: the presented bearer has to verify as
    a live key, it has to carry the `runner` scope, and its subject has to be this
    run. The third is what keeps a token minted for one run from reading another
    run's bundle, which matters because a bundle carries decrypted variables.
    """
    presented = bearer_credential(request)
    if not presented:
        raise unauthenticated()
    record = verify(presented, api_key_store())
    if record is None:
        raise unauthenticated()
    if RUNNER_SCOPE not in record.scopes:
        raise unauthenticated()
    if record.user_id != run_id:
        raise unauthenticated()
    return record


def require_run_token() -> Any:
    """Build the dependency gating the runner-only routes."""

    async def dependency(request: Request, run_id: str) -> ApiKeyRecord:
        """Return the verified run token bound to the run in the path."""
        return run_token_record(request, run_id)

    dependency.__name__ = "require_run_token"
    dependency.__doc__ = "The verified run token for the run named in this request's path."
    return dependency


class RunnerRoute(APIRoute):
    """A route class for the runner routes that hides their schema from strangers.

    FastAPI validates the path and body before a route's own checks run, so an
    unauthenticated caller would get a 422 naming every field the route expects.
    Here a validation failure becomes the same 401 an unauthenticated caller gets
    anywhere else, unless the request carries a run token for the run in its path,
    in which case the runner sees the 422 it needs to diagnose itself. The runner
    token route is reached without a run token, so it always answers 401.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the stock handler so a validation failure is judged by credential first."""
        handler = super().get_route_handler()
        route_path = self.path

        async def guarded(request: Request) -> Response:
            """Run the stock handler, turning an unauthenticated 422 into a 401."""
            try:
                return await handler(request)
            except RequestValidationError:
                if route_path.endswith("/runner-token"):
                    raise unauthenticated() from None
                run_token_record(request, str(request.path_params.get("run_id", "")))
                raise

        return guarded


__all__ = [
    "ADMIN",
    "ALL_SCOPES",
    "CONFIGS_READ",
    "CONFIGS_WRITE",
    "RUNNER_REGISTRY_SCOPE",
    "RUNNER_SCOPE",
    "RUNS_APPLY",
    "RUNS_READ",
    "RUNS_WRITE",
    "REGISTRY_READ",
    "REGISTRY_WRITE",
    "RUN_KEY_RETENTION",
    "RUN_KEY_TTL_ATTRIBUTE",
    "RUN_KEY_USER_PREFIX",
    "RUN_TOKEN_TENANT",
    "RunnerRoute",
    "STATE_DOWNLOAD",
    "STEP_UP_MAX_AGE_SECONDS",
    "VARIABLES_READ",
    "VARIABLES_WRITE",
    "WORKSPACES_READ",
    "WORKSPACES_WRITE",
    "Depends",
    "api_key_store",
    "claims",
    "ensure_recent_auth",
    "device_grant_liveness",
    "key_owner_scopes",
    "reset_device_grant_liveness",
    "recent_auth",
    "require_run_token",
    "revoke_run_key",
    "run_token_record",
    "scopes",
    "sudo",
    "unauthenticated",
]
