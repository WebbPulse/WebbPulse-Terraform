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
from typing import Any, Callable, Coroutine, Final, Mapping

import anyio.from_thread
from fastapi import Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import Response
from webbpulse.identity import DeviceGrantLiveness, dynamo_device_grant_stores
from webbpulse.identity.api_keys import ApiKeyRecord, ApiKeyStore, DynamoApiKeyStore, is_api_key, verify
from webbpulse.identity.claims import AuthorizerClaims
from webbpulse.identity.scopes import (
    bearer_credential,
    claims_or_api_key,
    claims_scopes,
    is_api_key_actor,
    require_recent_auth,
    require_scopes,
)

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
STATE_WRITE: Final = "state:write"
"""Locking a workspace and writing its state from the CLI, which `terraform state mv`,
`import` and `force-unlock` need. Like `state:download`, never an ordinary grant."""
STATE_READ_OUTPUTS: Final = "state:read-outputs"
"""Reading another workspace's non-sensitive outputs, as HCP's remote state sharing does.

Like `state:download`, never an ordinary grant: an admin holds it, and a run's API token
carries it whenever its workspace grants `workspaces:read`, where the source workspace's
sharing settings decide which outputs that run may read."""
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
    STATE_WRITE,
    STATE_READ_OUTPUTS,
    REGISTRY_READ,
    REGISTRY_WRITE,
    ADMIN,
)
"""Every scope a human or an agent can hold, which is what the contract lists."""

WORKSPACES_FACTORY: Final = "workspaces:factory"
"""The factory grant: a run's API token may reach every workspace rather than only its own.

Only a run's API token holds it, and only when an admin who passed step-up put it in the
workspace's `run_api_token_scopes`, as the WebbPulse-Platform workspace's is for the factory
that manages every other workspace. Beside the write scopes it also creates workspaces,
writes the registry and stands in for step-up, which no other run token passes."""

RUN_API_TOKEN_SCOPES: Final = (
    WORKSPACES_READ,
    WORKSPACES_WRITE,
    VARIABLES_READ,
    VARIABLES_WRITE,
    REGISTRY_READ,
    REGISTRY_WRITE,
    WORKSPACES_FACTORY,
)
"""The scopes a workspace may grant its runs' API token, which a configuration using the
WebbPulse provider reads as `WEBBPULSE_TF_TOKEN`. Never admin, the runner scopes, raw state
or `runs:apply`, so a run cannot widen its own grant, read another run's bundle or approve
its own apply."""

RUN_API_WRITE_SCOPES: Final = frozenset({WORKSPACES_WRITE, VARIABLES_WRITE, REGISTRY_WRITE})
"""The grant's write scopes, which a plan phase's token never carries."""

RUN_TOKEN_WORKSPACE_BOUND_CODE: Final = "RUN_TOKEN_WORKSPACE_BOUND"
"""The stable code a run's API token is refused with outside its own workspace."""

RUN_TOKEN_STEP_UP_CODE: Final = "RUN_TOKEN_STEP_UP_REFUSED"
"""The stable code a run's API token is refused with on a change that needs step-up."""

RUN_API_TOKEN_KIND: Final = "run_api"
"""The `kind` a run's API token is minted with, which is what `key_owner_scopes` keys on."""

RUN_API_TOKEN_SCOPES_ATTRIBUTE: Final = "run_api_token_scopes"
"""The workspace attribute holding the scopes its runs' API token may exercise."""

RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE: Final = "run_api_token_scopes_version"
"""The workspace attribute saying which meaning its `run_api_token_scopes` was set under.

Absent on a grant set before the factory grant existed, when a grant holding every write
scope already reached every workspace. Every write of the grant stamps the current version."""

RUN_API_TOKEN_SCOPES_VERSION: Final = 2
"""The current meaning of `run_api_token_scopes`: cross-workspace reach only with `workspaces:factory`."""

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


def granted_run_api_scopes(workspace: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The run API token scopes a stored workspace grants, filtered to `RUN_API_TOKEN_SCOPES`."""
    stored = (workspace or {}).get(RUN_API_TOKEN_SCOPES_ATTRIBUTE) or []
    return tuple(scope for scope in RUN_API_TOKEN_SCOPES if scope in {str(value) for value in stored})


def effective_run_api_scopes(workspace: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The scopes a run's API token carries: the grant, plus `state:read-outputs` beside `workspaces:read`.

    Reading another workspace's outputs is then gated by that workspace's remote state
    sharing settings rather than by a separate grant on this one, as on HCP.
    """
    granted = granted_run_api_scopes(workspace)
    return (*granted, STATE_READ_OUTPUTS) if WORKSPACES_READ in granted else granted


def is_run_api_token(record: ApiKeyRecord) -> bool:
    """Whether a verified key is a run's API token rather than a person's or an agent's."""
    return (
        record.kind == RUN_API_TOKEN_KIND
        and record.tenant_id == RUN_TOKEN_TENANT
        and record.user_id.startswith(RUN_KEY_USER_PREFIX)
    )


def run_api_token_scopes(record: ApiKeyRecord) -> tuple[str, ...]:
    """What a run's API token may do right now: its workspace's current grant.

    Read live on every request, so clearing the workspace's grant or deleting the
    workspace takes effect at once. Ending the run revokes the key itself.
    """
    from ..db import repositories

    workspace_id = str(record.metadata.get("workspace_id", "") or "")
    if not workspace_id:
        return ()
    return effective_run_api_scopes(repositories.workspaces().get({"workspace_id": workspace_id}))


def key_owner_scopes(record: ApiKeyRecord) -> tuple[str, ...]:
    """The scopes a key's owner holds right now, read from the `users` table.

    Nothing for an owner that is gone, disabled or unverified, which is the same
    test `may_authenticate` applies at sign-in. Otherwise the scopes the owner's
    current role earns, the same ones a fresh session token would carry. A run's
    API token has no person behind it, so its workspace's grant stands in.
    """
    from ..db.users import UserRepository

    if is_run_api_token(record):
        return run_api_token_scopes(record)
    from ..identity.identity_hooks import ADMIN_ROLE, scope_claim_for_roles

    user = UserRepository().get(record.user_id)
    if user is None or user.disabled or not user.email_verified:
        return ()
    return tuple(scope_claim_for_roles([ADMIN_ROLE] if user.is_admin else []).split())


def is_run_api_claims(current: Mapping[str, Any]) -> bool:
    """Whether verified claims came from a run's API token: a key whose subject is a run."""
    return is_api_key_actor(current) and str(current.get("sub", "") or "").startswith(RUN_KEY_USER_PREFIX)


def holds_factory_grant(current: Mapping[str, Any]) -> bool:
    """Whether a run's API token holds the factory grant right now, read from its live claims."""
    return WORKSPACES_FACTORY in claims_scopes(current)


class RunApiBinding:
    """Which workspaces a request's run API token may reach: `workspace_id`, or every one for a factory."""

    def __init__(self, workspace_id: str, factory: bool) -> None:
        """Bind to `workspace_id`, or to none in particular when `factory`."""
        self.workspace_id = workspace_id
        self.factory = factory

    def allows(self, workspace_id: str) -> bool:
        """Whether this token may act on `workspace_id`."""
        return self.factory or (bool(self.workspace_id) and workspace_id == self.workspace_id)


_BINDING_STATE: Final = "run_api_binding"


def run_api_binding(request: Request) -> RunApiBinding | None:
    """The binding of this request's run API token, or None for any other caller.

    Verified once per request and cached on it. The factory grant counts only when the
    key was minted with it and the workspace still grants it, the same intersection the
    claims carry.
    """
    if hasattr(request.state, _BINDING_STATE):
        cached: RunApiBinding | None = getattr(request.state, _BINDING_STATE)
        return cached
    binding: RunApiBinding | None = None
    presented = bearer_credential(request)
    if presented and is_api_key(presented):
        record = verify(presented, api_key_store(), touch=False)
        if record is not None and is_run_api_token(record):
            live = set(run_api_token_scopes(record)) & set(record.scopes)
            binding = RunApiBinding(
                str(record.metadata.get("workspace_id", "") or ""),
                factory=WORKSPACES_FACTORY in live,
            )
    setattr(request.state, _BINDING_STATE, binding)
    return binding


def workspace_bound() -> HTTPException:
    """The 403 a run's API token gets outside its own workspace."""
    return HTTPException(
        status_code=403,
        detail={
            "message": "A run's API token reaches only its own workspace.",
            "error_code": RUN_TOKEN_WORKSPACE_BOUND_CODE,
        },
    )


def ensure_run_api_reach(request: Request, workspace_id: str) -> None:
    """Refuse a run API token acting on a workspace other than its own, unless it is the factory's."""
    binding = run_api_binding(request)
    if binding is not None and not binding.allows(workspace_id):
        raise workspace_bound()


def bound_workspace_id(request: Request) -> str | None:
    """The one workspace a listing may show this caller, or None to show every one."""
    binding = run_api_binding(request)
    if binding is None or binding.factory:
        return None
    return binding.workspace_id


_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


async def run_api_workspace_binding(request: Request) -> None:
    """The one dependency binding a run's API token to its own workspace, on every workspace-scoped router.

    A route naming a `workspace_id` in its path is refused for any other workspace. A
    write naming none, such as creating a workspace or a project or connecting a registry
    module, is a list-wide write and refused outright. Reads naming none filter or check
    for themselves through `bound_workspace_id` and `ensure_run_api_reach`. A token holding
    the factory grant passes, and any other caller is untouched.
    """
    binding = run_api_binding(request)
    if binding is None or binding.factory:
        return
    workspace_id = request.path_params.get("workspace_id")
    if workspace_id is not None:
        if not binding.allows(str(workspace_id)):
            raise workspace_bound()
        return
    if request.method.upper() not in _READ_METHODS:
        raise workspace_bound()


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

A person whose login is older gets a 401 `STEP_UP_REQUIRED` and confirms with a passkey, an
authenticator code or their password through `/api/auth/step-up`, after which the browser
replays the call. An agent key has no login to age and passes; its scopes are what limit it.
Confirming a run is not gated: `runs:apply` on a live session is enough, as on HCP."""


def refuse_run_token_step_up(current: Mapping[str, Any]) -> None:
    """Refuse a run's API token at a step-up gate, unless it holds the factory grant.

    The package gate passes every key, since a key has no login to age. A run's token is
    the one key whose holder is whatever code the run executes, so it never stands in for
    a person's recent login; only the factory grant, which an admin gave on purpose, does.
    """
    if is_run_api_claims(current) and not holds_factory_grant(current):
        raise HTTPException(
            status_code=403,
            detail={
                "message": "A run's API token cannot make a change that needs step-up.",
                "error_code": RUN_TOKEN_STEP_UP_CODE,
            },
        )


def _step_up(claims_dependency: Any) -> Any:
    """The package step-up gate over `claims_dependency`, refusing run tokens without the factory grant."""
    gate = require_recent_auth(STEP_UP_MAX_AGE_SECONDS, claims_dependency=claims_dependency)

    async def dependency(current: AuthorizerClaims = Depends(gate)) -> AuthorizerClaims:
        """Return the claims once the login is recent and the caller is not a bound run token."""
        refuse_run_token_step_up(current)
        return current

    dependency.__name__ = "require_recent_auth"
    dependency.__doc__ = "Requires a recent login, and refuses a run's API token without the factory grant."
    return dependency


_package_recent_auth = require_recent_auth(STEP_UP_MAX_AGE_SECONDS, claims_dependency=claims)

recent_auth = _step_up(claims)
"""The step-up gate on its own, for a route that already checks its caller another way."""


def sudo(*required: str) -> Any:
    """A dependency requiring `required` and then a login within `STEP_UP_MAX_AGE_SECONDS`.

    The scope check runs first, so a caller who could never make the change gets a 403
    rather than a password prompt that leads nowhere.
    """
    return _step_up(scopes(*required))


def ensure_recent_auth(current: AuthorizerClaims) -> None:
    """Apply the step-up gate from inside a sync route, for a change only sometimes sensitive.

    Such as a PATCH that happens to change the run role: the route decides from the body
    and the stored row, then raises the same 401 `recent_auth` would.
    """
    anyio.from_thread.run(_recent_auth_check, current)


async def _recent_auth_check(current: AuthorizerClaims) -> None:
    """Run the package gate and the run token refusal against claims this request already resolved."""
    await _package_recent_auth(current)
    refuse_run_token_step_up(current)


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
    "RUN_API_TOKEN_KIND",
    "RUN_API_TOKEN_SCOPES",
    "RUN_API_TOKEN_SCOPES_ATTRIBUTE",
    "RUN_API_TOKEN_SCOPES_VERSION",
    "RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE",
    "RUN_API_WRITE_SCOPES",
    "RUN_TOKEN_STEP_UP_CODE",
    "RUN_TOKEN_WORKSPACE_BOUND_CODE",
    "RunApiBinding",
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
    "STATE_READ_OUTPUTS",
    "STATE_WRITE",
    "STEP_UP_MAX_AGE_SECONDS",
    "VARIABLES_READ",
    "VARIABLES_WRITE",
    "WORKSPACES_READ",
    "WORKSPACES_FACTORY",
    "WORKSPACES_WRITE",
    "Depends",
    "api_key_store",
    "bound_workspace_id",
    "claims",
    "ensure_run_api_reach",
    "holds_factory_grant",
    "is_run_api_claims",
    "refuse_run_token_step_up",
    "run_api_binding",
    "run_api_workspace_binding",
    "workspace_bound",
    "ensure_recent_auth",
    "device_grant_liveness",
    "effective_run_api_scopes",
    "granted_run_api_scopes",
    "is_run_api_token",
    "key_owner_scopes",
    "reset_device_grant_liveness",
    "recent_auth",
    "require_run_token",
    "revoke_run_key",
    "run_api_token_scopes",
    "run_token_record",
    "scopes",
    "sudo",
    "unauthenticated",
]
