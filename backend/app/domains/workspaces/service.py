"""Workspace, variable and config version storage.

The uniqueness of a workspace name is enforced by a conditional write against the
`by_name` GSI read, not by the index itself: DynamoDB has no unique index, so the
claim is a condition on the item plus a query that refuses a duplicate. Two
concurrent creates of the same name can both pass the query, so the condition on
`workspace_id` is what keeps the table from holding two rows with one id, and the
query is what makes the ordinary case a clean 409.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.core import variable_cipher
from ...common.db import repositories
from ...common.db.tables import (
    CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
    WORKSPACES_BY_NAME_INDEX,
)
from ...common.runs.workspace_runs import RunStillActive, delete_workspace_runs, require_no_active_run
from ...common.workspaces import reads
from ...common.workspaces.reads import (
    CONFIG_VERSION_ID_PREFIX,
    WORKSPACE_ID_PREFIX,
    ConfigVersionNotFound,
    RunRoleMissing,
    VariableNotFound,
    WorkspaceNotFound,
    config_key,
    config_object_exists,
    get_variable,
    get_workspace,
    list_variables,
    resolved_variables,
)
from . import hcl, state_versions
from .schemas.workspace import CLEARABLE_WORKSPACE_FIELDS

__all__ = [
    "CONFIG_VERSION_ID_PREFIX",
    "WORKSPACE_ID_PREFIX",
    "ConfigVersionNotFound",
    "HclNotAllowed",
    "RunRoleMissing",
    "RunStillActive",
    "VariableNotFound",
    "WorkspaceNameTaken",
    "WorkspaceManagesResources",
    "WorkspaceNotFound",
    "config_key",
    "config_object_exists",
    "get_variable",
    "get_workspace",
    "list_variables",
    "resolved_variables",
]
"""The reads this domain shares with the runs function are re-exported from
`app.common.workspaces.reads`, so this module's public surface and every error
code raised against it are unchanged."""

CONFIG_CONTENT_TYPE: Final = "application/gzip"
"""The one content type a config tarball may declare, signed into the PUT."""

CONFIG_UPLOAD_EXPIRES_IN: Final = 900
"""Fifteen minutes for the client to start its upload, the package's own default."""

IAM_ROLE_NAME_MAX_LENGTH: Final = 64
"""The IAM ceiling on a role name. The derived name has to fit inside it."""


class WorkspaceNameTaken(Exception):
    """Another workspace already holds this name."""


class HclNotAllowed(Exception):
    """An `env` variable was marked HCL, which has no meaning.

    A process environment variable is a string to the process, so there is nothing
    that would parse the expression. Refused rather than ignored, because silently
    dropping the flag would store a value whose rendering does not match what the
    caller asked for.
    """


def run_role_name(workspace_id: str, *, settings: Settings | None = None) -> str:
    """The role name for one workspace, inside the runner's AssumeRole grant.

    The `ws-` prefix is dropped so the ULID alone follows the stack prefix, which
    keeps the name inside the IAM ceiling of sixty four characters.
    """
    resolved = settings or get_settings()
    return f"{resolved.RUN_ROLE_NAME_PREFIX}{workspace_id.removeprefix(WORKSPACE_ID_PREFIX)}"


def run_role_setup(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """The three values a person needs to build one workspace's run role."""
    resolved = settings or get_settings()
    principals = resolved.runner_task_role_arns
    return {
        "principal_arn": principals[0] if principals else "",
        "principal_arns": principals,
        "external_id": workspace_id,
        "role_name": run_role_name(workspace_id, settings=resolved),
    }


def render_workspace(item: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """One stored workspace row as the API returns it, with its run role setup."""
    resolved = settings or get_settings()
    workspace_id = str(item["workspace_id"])
    return dict(item) | {"run_role_setup": run_role_setup(workspace_id, settings=resolved)}


def create_workspace(payload: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Store a new workspace, refusing a name another workspace holds."""
    resolved = settings or get_settings()
    repository = repositories.workspaces(resolved)
    name = str(payload["name"])
    if find_by_name(name, settings=resolved) is not None:
        raise WorkspaceNameTaken(name)

    item: dict[str, Any] = {
        "workspace_id": f"{WORKSPACE_ID_PREFIX}{new_ulid()}",
        "name": name,
        "engine": payload.get("engine", "terraform"),
        "engine_version": payload["engine_version"],
        "run_role_arn": payload.get("run_role_arn") or None,
        "working_directory": payload.get("working_directory", "") or "",
        "description": payload.get("description", "") or "",
        "trigger_patterns": list(payload.get("trigger_patterns") or []),
        "speculative_plans": bool(payload.get("speculative_plans", True)),
        "created_at": now_iso(),
    }
    if payload.get("vcs_repo"):
        item["vcs_repo"] = str(payload["vcs_repo"])
        item["vcs_repo_key"] = str(payload["vcs_repo"]).lower()
    if payload.get("tracked_branch"):
        item["tracked_branch"] = str(payload["tracked_branch"])
    try:
        repository.put(item, condition=Attr("workspace_id").not_exists())
    except ConditionFailed as error:
        raise WorkspaceNameTaken(name) from error
    return item


def find_by_name(name: str, *, settings: Settings | None = None) -> dict[str, Any] | None:
    """The workspace holding this name, through the `by_name` index, or `None`."""
    resolved = settings or get_settings()
    page = repositories.workspaces(resolved).query(
        Key("name").eq(name),
        index_name=WORKSPACES_BY_NAME_INDEX,
        limit=1,
    )
    return page.items[0] if page.items else None


def list_workspaces(*, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every workspace, oldest first.

    A scan rather than a query: the contract sizes this at seven workspaces, and
    a table with no collection key has nothing to query on.
    """
    resolved = settings or get_settings()
    items = list(repositories.workspaces(resolved).iter_scan())
    return sorted(items, key=lambda item: str(item.get("workspace_id", "")))


def update_workspace(
    workspace_id: str,
    changes: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Apply a partial edit to one workspace, or `WorkspaceNotFound`.

    `changes` is JSON Merge Patch: a key the request body did not carry is absent
    from the mapping and is left untouched, and a key carrying an explicit null
    clears that field. The router builds it with `model_dump(exclude_unset=True)`,
    which is what makes the two distinguishable at all, and only the fields in
    `CLEARABLE_WORKSPACE_FIELDS` may be cleared.

    A null becomes a DynamoDB REMOVE rather than a stored null, so a cleared field
    reads back as its declared default and no row carries a null attribute.

    A change to `run_role_arn` drops the recorded check outcome in the same write:
    the previous success belonged to the previous role, and leaving it behind would
    show a new, unchecked role as connected. Clearing the ARN counts as a change,
    so the outcome goes with it.

    A change to `vcs_repo` rewrites the lowercased `vcs_repo_key` the binding
    index reads and drops the recorded `vcs_repository_id`, which belonged to the
    previous repository. Clearing it removes both.
    """
    resolved = settings or get_settings()
    assignments = {key: value for key, value in changes.items() if value is not None}
    clears = [key for key, value in changes.items() if value is None and key in CLEARABLE_WORKSPACE_FIELDS]
    if not assignments and not clears:
        return get_workspace(workspace_id, settings=resolved)

    existing = get_workspace(workspace_id, settings=resolved)
    role_changed = "run_role_arn" in changes and changes["run_role_arn"] != existing.get("run_role_arn")

    assignments["updated_at"] = now_iso()
    removals = [*clears]
    if role_changed:
        removals.extend(("run_role_checked_at", "run_role_account_id"))
    if "vcs_repo" in changes and changes["vcs_repo"] != existing.get("vcs_repo"):
        if existing.get("vcs_repository_id") is not None:
            removals.append("vcs_repository_id")
        if changes["vcs_repo"] is None:
            removals.append("vcs_repo_key")
        else:
            assignments["vcs_repo_key"] = str(changes["vcs_repo"]).lower()

    names = {f"#{key}": key for key in (*assignments, *removals)}
    values = {f":{key}": value for key, value in assignments.items()}
    expression = "SET " + ", ".join(f"#{key} = :{key}" for key in assignments)
    if removals:
        expression += " REMOVE " + ", ".join(f"#{key}" for key in removals)
    repository = repositories.workspaces(resolved)
    try:
        updated = repository.update(
            {"workspace_id": workspace_id},
            update_expression=expression,
            expression_names=names,
            expression_values=values,
            condition=Attr("workspace_id").exists(),
            return_values="ALL_NEW",
        )
    except ConditionFailed as error:
        raise WorkspaceNotFound(workspace_id) from error
    if updated is None:
        raise WorkspaceNotFound(workspace_id)
    return updated


class WorkspaceManagesResources(Exception):
    """The workspace's current state still tracks resources, so a safe delete refuses."""


def delete_workspace(workspace_id: str, *, force: bool = False, settings: Settings | None = None) -> None:
    """Delete one workspace with its finished runs, current state and variables.

    Every check runs before anything is removed: `WorkspaceNotFound`, then
    `RunStillActive` for a run that has not finished, then, unless `force`,
    `WorkspaceManagesResources` when the current state tracks an instance. The
    deletes then run runs, state, variables and the workspace row last, so a
    failure part way leaves the workspace in place and a retry finishes the job.
    Removing the state object leaves a delete marker over its versions, and the
    config versions and their tarballs are left to the artifacts bucket lifecycle.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    require_no_active_run(workspace_id, settings=resolved)
    if not force and state_versions.current_state_manages_resources(workspace_id, settings=resolved):
        raise WorkspaceManagesResources(workspace_id)
    delete_workspace_runs(workspace_id, settings=resolved)
    state_versions.delete_current_state(workspace_id, settings=resolved)
    variables_repository = repositories.variables(resolved)
    keys = [
        {"workspace_id": workspace_id, "key": str(item["key"])}
        for item in variables_repository.iter_query(Key("workspace_id").eq(workspace_id))
    ]
    if keys:
        variables_repository.delete_many(keys)
    repositories.workspaces(resolved).delete({"workspace_id": workspace_id})


def put_variable(
    workspace_id: str,
    key: str,
    payload: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Set one variable, sealing the value when it is marked sensitive.

    A sensitive value never reaches the table in the clear, and the plaintext is
    not returned: the caller gets the row as the API renders it, with `value`
    absent.

    An HCL value is validated before it is stored, so a broken expression is
    refused here rather than failing every subsequent run on the workspace. The
    validation happens ahead of the sealing so the message can never carry any of
    a sensitive value back.

    Raises:
        WorkspaceNotFound: No such workspace.
        HclNotAllowed: An `env` variable was marked HCL.
        hcl.InvalidHcl: The expression cannot parse.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    existing = repositories.variables(resolved).get({"workspace_id": workspace_id, "key": key})

    sensitive = bool(payload.get("sensitive", False))
    category = str(payload.get("category", "terraform"))
    is_hcl = bool(payload.get("hcl", False))
    value = str(payload["value"])
    if is_hcl and category != "terraform":
        raise HclNotAllowed(key)
    if is_hcl:
        hcl.validate_name(key)
        hcl.validate(value)

    item: dict[str, Any] = {
        "workspace_id": workspace_id,
        "key": key,
        "category": category,
        "sensitive": sensitive,
        "hcl": is_hcl,
        "description": payload.get("description", "") or "",
        "created_at": str(existing["created_at"]) if existing else now_iso(),
    }
    if existing:
        item["updated_at"] = now_iso()

    if sensitive:
        item.update(variable_cipher.seal(value, workspace_id=workspace_id, key=key, settings=resolved))
    else:
        item["value"] = value

    repositories.variables(resolved).put(item)
    return item


def delete_variable(workspace_id: str, key: str, *, settings: Settings | None = None) -> None:
    """Delete one variable, or `VariableNotFound`."""
    resolved = settings or get_settings()
    get_variable(workspace_id, key, settings=resolved)
    repositories.variables(resolved).delete({"workspace_id": workspace_id, "key": key})


def render_variable(item: dict[str, Any]) -> dict[str, Any]:
    """One stored variable row as the API returns it, with no sealed fields.

    A sensitive variable's `value` is `None` rather than absent, so a client can
    tell "withheld" from "empty string" without reading the `sensitive` flag.

    `hcl` is read with a default, because every row written before the flag
    existed carries no such attribute and those values are literal.
    """
    sensitive = bool(item.get("sensitive", False))
    return {
        "workspace_id": str(item["workspace_id"]),
        "key": str(item["key"]),
        "value": None if sensitive else str(item.get("value", "")),
        "category": str(item.get("category", "terraform")),
        "sensitive": sensitive,
        "hcl": bool(item.get("hcl", False)),
        "description": str(item.get("description", "") or ""),
        "created_at": str(item.get("created_at", "")),
        "updated_at": str(item["updated_at"]) if item.get("updated_at") else None,
    }


def create_config_version(
    workspace_id: str,
    size_bytes: int,
    *,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], Any]:
    """Store a pending config version and mint the presigned PUT for its tarball.

    The row is written before the URL is minted, so an upload that never happens
    leaves a `pending` row a person can see rather than a signed URL nothing
    recorded. Nothing tells the control plane when the client's PUT lands, so the
    row stays `pending` until a read reconciles it against the object: see
    `reconcile_config_version`.
    """
    from webbpulse.storage import presigned_put

    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)

    config_version_id = f"{CONFIG_VERSION_ID_PREFIX}{new_ulid()}"
    key = config_key(workspace_id, config_version_id)
    item: dict[str, Any] = {
        "config_version_id": config_version_id,
        "workspace_id": workspace_id,
        "key": key,
        "status": "pending",
        "size_bytes": int(size_bytes),
        "created_at": now_iso(),
    }
    repositories.config_versions(resolved).put(item)

    upload = presigned_put(
        resolved.ARTIFACTS_BUCKET,
        key,
        CONFIG_CONTENT_TYPE,
        int(size_bytes),
        CONFIG_UPLOAD_EXPIRES_IN,
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.s3_endpoint_url,
    )
    return item, upload


def _uploaded_writer(settings: Settings) -> Callable[[str], None]:
    """The writer that persists the `uploaded` flip, which only this domain holds.

    Handed to the shared read so the read itself stays free of writes: the runs
    function reaches the same read under a role with no write grant on this table.
    """

    def persist(config_version_id: str) -> None:
        """Move one config version to `uploaded`."""
        mark_config_version_uploaded(config_version_id, settings=settings)

    return persist


def reconcile_config_version(
    item: dict[str, Any],
    *,
    persist: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Move a `pending` row to `uploaded` once its object is in the bucket.

    The bucket check lives in `app.common.workspaces.reads`, which both functions
    reach. This wrapper is what adds the write: with `persist` true it hands the
    shared read this domain's writer, so the flip is stored as well as returned.

    With `persist` false the bucket is still consulted and the returned row still
    reads `uploaded`, but nothing is written. That is for the runs function, whose
    role holds a read only grant on this table by design.
    """
    resolved = settings or get_settings()
    return reads.reconcile_config_version(
        item,
        persist=_uploaded_writer(resolved) if persist else None,
        settings=resolved,
    )


def get_config_version(
    workspace_id: str,
    config_version_id: str,
    *,
    persist: bool = True,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One config version, or `ConfigVersionNotFound`.

    A row belonging to another workspace reads as absent rather than as a 403, so
    nothing here confirms that an id a caller guessed exists elsewhere.

    `persist` is handed to `reconcile_config_version`: a caller reading these rows
    under a read only grant, as the runs function does, passes false and gets the
    bucket's truth without the write.
    """
    resolved = settings or get_settings()
    return reads.get_config_version(
        workspace_id,
        config_version_id,
        persist=_uploaded_writer(resolved) if persist else None,
        settings=resolved,
    )


def list_config_versions(
    workspace_id: str,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """One workspace's config versions, oldest first."""
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    return [
        reconcile_config_version(item, settings=resolved)
        for item in repositories.config_versions(resolved).iter_query(
            Key("workspace_id").eq(workspace_id),
            index_name=CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
        )
    ]


def mark_config_version_uploaded(
    config_version_id: str,
    *,
    settings: Settings | None = None,
) -> None:
    """Move a config version to `uploaded`, tolerating a row already there."""
    resolved = settings or get_settings()
    repositories.config_versions(resolved).update(
        {"config_version_id": config_version_id},
        update_expression="SET #s = :s, updated_at = :now",
        expression_names={"#s": "status"},
        expression_values={":s": "uploaded", ":now": now_iso()},
        condition=Attr("config_version_id").exists(),
    )
