"""Workspace, variable and config version storage.

The uniqueness of a workspace name is enforced by a conditional write against the
`by_name` GSI read, not by the index itself: DynamoDB has no unique index, so the
claim is a condition on the item plus a query that refuses a duplicate. Two
concurrent creates of the same name can both pass the query, so the condition on
`workspace_id` is what keeps the table from holding two rows with one id, and the
query is what makes the ordinary case a clean 409.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Final, Literal

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.core import variable_cipher
from ...common.core.auth import (
    RUN_API_TOKEN_SCOPES_ATTRIBUTE,
    RUN_API_TOKEN_SCOPES_VERSION,
    RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE,
)
from ...common.db import repositories
from ...common.db.tables import (
    CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
    WORKSPACES_BY_NAME_INDEX,
)
from ...common.notifications import store as notification_store
from ...common.runs.workspace_runs import (
    RunStillActive,
    delete_workspace_runs,
    latest_run_summary,
    latest_runs,
    require_no_active_run,
    workspace_run_ids,
)
from ...common.workspaces import aws_connect, cleanup, hcl, reads
from ...common.workspaces import readme as config_readme
from ...common.workspaces import vcs as workspace_vcs
from ...common.workspaces.projects import DEFAULT_PROJECT_ID, project_of
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
from . import projects as project_store
from . import state_versions, vcs_connect
from .schemas.workspace import CLEARABLE_WORKSPACE_FIELDS
from .vcs_connect import RepositoryNotInstalled

_log = logging.getLogger(__name__)

__all__ = [
    "CONFIG_VERSION_ID_PREFIX",
    "WORKSPACE_ID_PREFIX",
    "ConfigVersionNotFound",
    "HclNotAllowed",
    "ProjectNotFound",
    "RemoteStateConsumerNotFound",
    "RepositoryNotInstalled",
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

TFE_CONFIG_MAX_BYTES: Final = 250_000_000
"""The ceiling on a `tfe.v2` upload, the largest the `/api/v1` create accepts.

go-tfe sends no length up front, so it is checked against the object once it lands."""

_PRIVATE_WORKSPACE_FIELDS: Final = frozenset(
    {aws_connect.TOKEN_HASH_ATTRIBUTE, aws_connect.TOKEN_EXPIRES_ATTRIBUTE, RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE}
)
"""Row attributes no response carries, the connect token's hash above all."""

GLOBAL_REMOTE_STATE_FIELD: Final = "global_remote_state"
"""The workspace attribute sharing its non-sensitive outputs with every workspace's runs."""

REMOTE_STATE_CONSUMERS_FIELD: Final = "remote_state_consumer_ids"
"""The workspace attribute naming the workspaces whose runs may read its non-sensitive outputs."""

IAM_ROLE_NAME_MAX_LENGTH: Final = 64
"""The IAM ceiling on a role name. The derived name has to fit inside it."""


class WorkspaceNameTaken(Exception):
    """Another workspace already holds this name."""


class RemoteStateConsumerNotFound(Exception):
    """A remote state sharing edit named a workspace that does not exist."""


ProjectNotFound = project_store.ProjectNotFound
"""A create or move named a project that does not exist."""

WorkspaceSort = Literal["name", "-name", "-latest_run", "-updated_at", "-created_at", "status"]
"""The orders the workspace list can return, a leading `-` meaning newest or last first.

`-updated_at` is "last updated", the `latest_change_at` the list shows. `status` puts the
runs that need someone first: waiting on a confirmation, then errored, then still going,
then settled, and workspaces that never ran last."""

_STATUS_ATTENTION: Final = {
    "awaiting_confirmation": 0,
    "planned": 0,
    "errored": 1,
    "pending": 2,
    "planning": 2,
    "applying": 2,
}
"""How urgently a latest run status needs someone, lower first. Any other status is settled."""

_SETTLED_RANK: Final = 3
_NEVER_RAN_RANK: Final = 4


class HclNotAllowed(Exception):
    """An `env` variable was marked HCL, which has no meaning.

    A process environment variable is a string to the process, so there is nothing
    that would parse the expression. Refused rather than ignored, because silently
    dropping the flag would store a value whose rendering does not match what the
    caller asked for.
    """


def run_role_name(workspace_id: str, *, settings: Settings | None = None) -> str:
    """The role name for one workspace, shared with the runs function's connect check."""
    return aws_connect.run_role_name(workspace_id, settings=settings)


def run_role_setup(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """The three values a person needs to build one workspace's run role.

    The principal is the control plane's credential vending role, the only one a
    run role may trust: no runner task can assume a workspace role itself.
    """
    resolved = settings or get_settings()
    principals = resolved.run_role_principal_arns
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
    rendered = {key: value for key, value in item.items() if key not in _PRIVATE_WORKSPACE_FIELDS}
    return rendered | {
        "project_id": project_of(item),
        "run_role_setup": run_role_setup(workspace_id, settings=resolved),
        "run_role_reconnect_required": aws_connect.reconnect_required(item),
    }


def _connection(
    repository: str,
    *,
    branch_given: bool,
    settings: Settings,
) -> dict[str, Any]:
    """The attributes connecting a workspace to `repository` writes.

    Resolved through the GitHub App when the environment has one, which records the
    repository id, the installation and the canonical name, and fills the tracked
    branch with the default branch when the request named none. Without an App only
    the name is written and the first upload records the id.
    """
    found = vcs_connect.resolve_repository(repository, settings=settings)
    if found is None:
        return {"vcs_repo": repository, "vcs_repo_key": workspace_vcs.repo_key(repository)}
    attributes: dict[str, Any] = {
        "vcs_repo": found.full_name,
        "vcs_repo_key": workspace_vcs.repo_key(found.full_name),
        "vcs_repository_id": found.repository_id,
        "vcs_installation_id": found.installation_id,
    }
    if not branch_given and found.default_branch:
        attributes["tracked_branch"] = found.default_branch
    return attributes


def create_workspace(payload: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Store a new workspace, refusing a name another workspace holds.

    A `vcs_repo` is resolved through the GitHub App before anything is written, so a
    repository the App cannot see is refused with `RepositoryNotInstalled`, and a
    `project_id` that names no project with `ProjectNotFound`.
    """
    resolved = settings or get_settings()
    repository = repositories.workspaces(resolved)
    name = str(payload["name"])
    if find_by_name(name, settings=resolved) is not None:
        raise WorkspaceNameTaken(name)
    project_id = str(payload.get("project_id") or DEFAULT_PROJECT_ID)
    project_store.require_project(project_id, settings=resolved)

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
        "file_triggers_enabled": bool(payload.get("file_triggers_enabled", True)),
        "auto_apply": bool(payload.get("auto_apply", False)),
        "plan_assume_role_arns": list(payload.get("plan_assume_role_arns") or []),
        "plan_secret_arns": list(payload.get("plan_secret_arns") or []),
        "created_at": now_iso(),
    }
    if project_id != DEFAULT_PROJECT_ID:
        item["project_id"] = project_id
    if payload.get("plan_role_arn"):
        item["plan_role_arn"] = str(payload["plan_role_arn"])
    if payload.get("tracked_branch"):
        item["tracked_branch"] = str(payload["tracked_branch"])
    if payload.get("vcs_repo"):
        item |= _connection(
            str(payload["vcs_repo"]),
            branch_given=bool(payload.get("tracked_branch")),
            settings=resolved,
        )
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


def list_workspace_items(
    *,
    project_id: str | None = None,
    search: str | None = None,
    sort: WorkspaceSort | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Workspaces rendered for the list, each with its newest run and latest change.

    `project_id` keeps one project's workspaces and `search` those whose name holds it,
    ignoring case. Without a `sort` the order stays oldest first. The newest runs come
    from one walk of the runs table for the whole list, never a read per workspace.
    """
    resolved = settings or get_settings()
    items = list_workspaces(settings=resolved)
    if project_id is not None:
        items = [item for item in items if project_of(item) == project_id]
    if search:
        needle = search.casefold()
        items = [item for item in items if needle in str(item.get("name", "")).casefold()]
    runs = latest_runs([str(item["workspace_id"]) for item in items], settings=resolved)
    rendered: list[dict[str, Any]] = []
    for item in items:
        run = runs.get(str(item["workspace_id"]))
        latest = None if run is None else latest_run_summary(run)
        changed_at = latest["changed_at"] if latest else str(item.get("updated_at") or item.get("created_at") or "")
        extra: dict[str, Any] = {"latest_run": latest, "latest_change_at": changed_at}
        rendered.append(render_workspace(item, settings=resolved) | extra)
    return rendered if sort is None else sort_workspace_items(rendered, sort)


def _attention_rank(item: dict[str, Any]) -> int:
    """Where a workspace's newest run falls in the `status` order."""
    latest = item.get("latest_run")
    if not latest:
        return _NEVER_RAN_RANK
    return _STATUS_ATTENTION.get(str(latest.get("status", "")), _SETTLED_RANK)


def _created_key(item: dict[str, Any]) -> tuple[str, str]:
    """Creation time, with the time ordered id breaking a tie inside one second."""
    return str(item.get("created_at", "")), str(item["workspace_id"])


def sort_workspace_items(items: list[dict[str, Any]], sort: WorkspaceSort) -> list[dict[str, Any]]:
    """The list items in one of the `WorkspaceSort` orders.

    Every order ends on the name, so equal keys always come back the same way. Each
    pass is a stable sort, which is what lets a descending key sit beside an ascending
    tie break.
    """
    ordered = sorted(items, key=lambda item: (str(item.get("name", "")).casefold(), str(item["workspace_id"])))
    if sort == "name":
        return ordered
    if sort == "-name":
        return ordered[::-1]
    if sort == "-created_at":
        return sorted(ordered, key=_created_key, reverse=True)
    if sort == "-updated_at":
        return sorted(ordered, key=lambda item: str(item.get("latest_change_at", "")), reverse=True)
    if sort == "-latest_run":
        ran = [item for item in ordered if item.get("latest_run")]
        never = [item for item in ordered if not item.get("latest_run")]
        return sorted(ran, key=lambda item: str(item["latest_run"].get("created_at", "")), reverse=True) + never
    by_change = sorted(ordered, key=lambda item: str(item.get("latest_change_at", "")), reverse=True)
    return sorted(by_change, key=_attention_rank)


def _stage_run_role(changes: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    """The edit with a staged role resolved against the role the workspace runs as now."""
    if "run_role_arn" in changes:
        return {**changes, "pending_run_role_arn": None}
    pending = changes.get("pending_run_role_arn")
    if pending is None:
        return changes
    current = str(existing.get("run_role_arn") or "")
    if current and current != pending:
        return changes
    return {**changes, "run_role_arn": pending, "pending_run_role_arn": None}


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

    A `pending_run_role_arn` stages a role without switching to it: runs keep the
    current role until the run role check sees a verification run assume the new
    one. A workspace with no role, or already on that role, takes it at once
    instead, since there is nothing to keep working. Setting `run_role_arn`
    directly switches at once and discards whatever was staged, and a role other
    than the one a Quick setup stack connected also drops that connection and any
    waiting connect token, since the person chose another role by hand, along with
    the plan role that stack created unless the edit names a plan role itself.

    A `project_id` moves the workspace between projects and touches nothing else; the
    default project's id or a null moves it back to the default.

    A change to `vcs_repo` rewrites the lowercased `vcs_repo_key` the binding
    index reads and resolves the repository through the GitHub App, which records
    its id, installation and canonical name, and fills `tracked_branch` with the
    default branch when the request carries no branch. Without an App the recorded
    id and installation are dropped instead, since they belonged to the previous
    repository. Clearing `vcs_repo` removes all of them.

    A change to `run_api_token_scopes`, a clear included, stamps the grant's current
    version, so the run API token's one-off factory migration never touches a grant an
    admin chose since.
    """
    resolved = settings or get_settings()
    if "project_id" in changes:
        changes = _resolve_project_move(changes, settings=resolved)
    if changes.get(REMOTE_STATE_CONSUMERS_FIELD):
        changes = _resolve_remote_state_consumers(workspace_id, changes, settings=resolved)
    if changes.get(GLOBAL_REMOTE_STATE_FIELD) is False:
        changes = {**changes, GLOBAL_REMOTE_STATE_FIELD: None}
    staged: dict[str, Any] | None = None
    if "run_role_arn" in changes or "pending_run_role_arn" in changes:
        staged = get_workspace(workspace_id, settings=resolved)
        changes = _stage_run_role(changes, staged)
    assignments = {key: value for key, value in changes.items() if value is not None}
    clears = [key for key, value in changes.items() if value is None and key in CLEARABLE_WORKSPACE_FIELDS]
    if not assignments and not clears:
        return get_workspace(workspace_id, settings=resolved)

    existing = staged if staged is not None else get_workspace(workspace_id, settings=resolved)
    role_changed = "run_role_arn" in changes and changes["run_role_arn"] != existing.get("run_role_arn")

    assignments["updated_at"] = now_iso()
    if RUN_API_TOKEN_SCOPES_ATTRIBUTE in changes and set(changes[RUN_API_TOKEN_SCOPES_ATTRIBUTE] or ()) != set(
        existing.get(RUN_API_TOKEN_SCOPES_ATTRIBUTE) or ()
    ):
        assignments[RUN_API_TOKEN_SCOPES_VERSION_ATTRIBUTE] = RUN_API_TOKEN_SCOPES_VERSION
    removals = [*clears]
    if role_changed:
        removals.extend(("run_role_checked_at", "run_role_account_id"))
        connected = existing.get(aws_connect.CONNECTION_ATTRIBUTE) or {}
        if connected and connected.get("role_arn") != changes["run_role_arn"]:
            removals.extend(
                (
                    aws_connect.CONNECTION_ATTRIBUTE,
                    aws_connect.TOKEN_HASH_ATTRIBUTE,
                    aws_connect.TOKEN_EXPIRES_ATTRIBUTE,
                )
            )
            stack_plan_role = connected.get(aws_connect.PLAN_ROLE_ATTRIBUTE)
            if (
                stack_plan_role
                and "plan_role_arn" not in changes
                and existing.get(aws_connect.PLAN_ROLE_ATTRIBUTE) == stack_plan_role
            ):
                removals.append(aws_connect.PLAN_ROLE_ATTRIBUTE)
    if "vcs_repo" in changes and not _repository_changed(changes["vcs_repo"], existing):
        assignments.pop("vcs_repo", None)
    elif "vcs_repo" in changes:
        connection: dict[str, Any] = {}
        if changes["vcs_repo"] is not None:
            same_repository = workspace_vcs.repo_key(str(changes["vcs_repo"])) == workspace_vcs.repo_key(
                str(existing.get("vcs_repo") or "")
            )
            connection = _connection(
                str(changes["vcs_repo"]),
                branch_given="tracked_branch" in changes or (same_repository and bool(existing.get("tracked_branch"))),
                settings=resolved,
            )
            assignments |= connection
        else:
            removals.append("vcs_repo_key")
        removals.extend(
            key
            for key in ("vcs_repository_id", "vcs_installation_id")
            if key not in connection and existing.get(key) is not None
        )

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


def _resolve_project_move(changes: dict[str, Any], *, settings: Settings) -> dict[str, Any]:
    """The edit with a move to the default project read as a clear, and any other target checked.

    The default project is the absence of `project_id`, so moving into it removes the
    attribute rather than storing its id. A project that does not exist raises
    `ProjectNotFound` before anything is written.
    """
    target = changes["project_id"]
    if target is None or target == DEFAULT_PROJECT_ID:
        return {**changes, "project_id": None}
    project_store.require_project(str(target), settings=settings)
    return changes


def _resolve_remote_state_consumers(
    workspace_id: str, changes: dict[str, Any], *, settings: Settings
) -> dict[str, Any]:
    """The edit with this workspace dropped from its consumers and every other one checked.

    An emptied list becomes a clear. A consumer that does not exist raises
    `RemoteStateConsumerNotFound` before anything is written.
    """
    consumers = [str(item) for item in changes[REMOTE_STATE_CONSUMERS_FIELD] if item != workspace_id]
    repository = repositories.workspaces(settings)
    missing = [item for item in consumers if repository.get({"workspace_id": item}) is None]
    if missing:
        raise RemoteStateConsumerNotFound(", ".join(missing))
    return {**changes, REMOTE_STATE_CONSUMERS_FIELD: consumers or None}


def _repository_changed(requested: Any, existing: dict[str, Any]) -> bool:
    """Whether a requested `vcs_repo` has to be written.

    The same repository in any case is left alone once its id is recorded, so a
    repeated save of a connected repository costs no GitHub call. A binding with no
    id yet is resolved again, which upgrades a name only binding once an App exists.
    """
    current = existing.get("vcs_repo")
    if requested is None or current is None:
        return requested != current
    same = workspace_vcs.repo_key(str(requested)) == workspace_vcs.repo_key(str(current))
    return not (same and existing.get("vcs_repository_id") is not None)


class WorkspaceManagesResources(Exception):
    """The workspace's current state still tracks resources, so a safe delete refuses."""


def delete_workspace(workspace_id: str, *, force: bool = False, settings: Settings | None = None) -> None:
    """Delete one workspace with everything it owns.

    Every check runs before anything is removed: `WorkspaceNotFound`, then
    `RunStillActive` for a run that has not finished, then, unless `force`,
    `WorkspaceManagesResources` when the current state tracks an instance.

    The object purge (run artifacts, config tarballs, and every state version, delete
    marker and lock under the workspace's state prefix) is queued first, while the run
    ids are still readable, and waits for the workspace row to be gone. The rows then
    go runs, config versions, variables and notification configurations first and the
    workspace row last, so a failure part way leaves the workspace in place with its
    state intact and a retry finishes the job. With no cleanup queue configured the purge runs inline at the end.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    require_no_active_run(workspace_id, settings=resolved)
    if not force and state_versions.current_state_manages_resources(workspace_id, settings=resolved):
        raise WorkspaceManagesResources(workspace_id)
    run_ids = workspace_run_ids(workspace_id, settings=resolved)
    queued = cleanup.enqueue(workspace_id, run_ids, settings=resolved)
    delete_workspace_runs(workspace_id, settings=resolved)
    config_versions_repository = repositories.config_versions(resolved)
    config_keys = [
        {"config_version_id": str(item["config_version_id"])}
        for item in config_versions_repository.iter_query(
            Key("workspace_id").eq(workspace_id), index_name=CONFIG_VERSIONS_BY_WORKSPACE_INDEX
        )
    ]
    if config_keys:
        config_versions_repository.delete_many(config_keys)
    variables_repository = repositories.variables(resolved)
    keys = [
        {"workspace_id": workspace_id, "key": str(item["key"])}
        for item in variables_repository.iter_query(Key("workspace_id").eq(workspace_id))
    ]
    if keys:
        variables_repository.delete_many(keys)
    notification_store.delete_workspace_configurations(workspace_id, settings=resolved)
    repositories.workspaces(resolved).delete({"workspace_id": workspace_id})
    if not queued:
        cleanup.purge(workspace_id, run_ids, settings=resolved)


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


def create_tfe_config_version(
    workspace_id: str,
    *,
    speculative: bool,
    auto_queue_runs: bool,
    provisional: bool = False,
    settings: Settings | None = None,
) -> tuple[dict[str, Any], str]:
    """Store a pending config version for `tfe.v2` and mint its unbounded PUT.

    go-tfe uploads with `Content-Type: application/octet-stream`, no length known at
    create time and no Authorization header, so the URL signs neither a type nor a
    length. The row carries `max_bytes` instead, and the reconcile refuses an object
    over it. `speculative` and `auto_queue_runs` are kept for the run that names it, and
    `provisional`, which `terraform plan -out` sends, is kept so it reads back as sent.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)

    config_version_id = f"{CONFIG_VERSION_ID_PREFIX}{new_ulid()}"
    key = config_key(workspace_id, config_version_id)
    item: dict[str, Any] = {
        "config_version_id": config_version_id,
        "workspace_id": workspace_id,
        "key": key,
        "status": "pending",
        "size_bytes": TFE_CONFIG_MAX_BYTES,
        "max_bytes": TFE_CONFIG_MAX_BYTES,
        "source": "api",
        "speculative": bool(speculative),
        "auto_queue_runs": bool(auto_queue_runs),
        "created_at": now_iso(),
    }
    if provisional:
        item["provisional"] = True
    repositories.config_versions(resolved).put(item)

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.s3_endpoint_url,
        config=Config(signature_version="s3v4"),
    )
    url = client.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": resolved.ARTIFACTS_BUCKET, "Key": key},
        ExpiresIn=CONFIG_UPLOAD_EXPIRES_IN,
        HttpMethod="PUT",
    )
    return item, str(url)


def _rejected_writer(settings: Settings) -> Callable[[str, str], None]:
    """The writer that refuses an oversized upload: the object goes, the row says why."""

    def reject(config_version_id: str, key: str) -> None:
        """Delete the object and record `upload_error` on the row."""
        cleanup.purge_prefix(settings.ARTIFACTS_BUCKET, key, settings=settings)
        repositories.config_versions(settings).update(
            {"config_version_id": config_version_id},
            update_expression="SET upload_error = :e, updated_at = :now",
            expression_values={":e": reads.UPLOAD_TOO_LARGE, ":now": now_iso()},
            condition=Attr("config_version_id").exists(),
        )

    return reject


def find_config_version(config_version_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One config version by id alone, reconciled and persisted, or `ConfigVersionNotFound`."""
    resolved = settings or get_settings()
    return reads.find_config_version(
        config_version_id,
        persist=_uploaded_writer(resolved),
        reject=_rejected_writer(resolved),
        settings=resolved,
    )


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
        reject=_rejected_writer(resolved) if persist else None,
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
        reject=_rejected_writer(resolved) if persist else None,
        settings=resolved,
    )


def get_config_version_detail(
    workspace_id: str,
    config_version_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One config version with its README, reading the README once if nothing has.

    A VCS config version gets its README at ingest. An API upload has no ingest
    step, since nothing tells the control plane when its PUT lands, so the first
    view of an uploaded one streams the tarball and stores the result on the row,
    found or not. Every later view reads the row alone. A tarball that cannot be
    read, expired say, is reported as no README and not marked, so nothing is
    stored that a later read could not confirm.
    """
    resolved = settings or get_settings()
    item = get_config_version(workspace_id, config_version_id, settings=resolved)
    if item.get("readme_scanned") or str(item.get("status")) != "uploaded":
        return item
    workspace = get_workspace(workspace_id, settings=resolved)
    try:
        found = config_readme.read_config_readme(
            resolved.ARTIFACTS_BUCKET,
            str(item["key"]),
            str(workspace.get("working_directory", "") or ""),
            settings=resolved,
        )
    except Exception:  # noqa: BLE001
        _log.warning(
            "Could not read the README from a config tarball.",
            extra={"event": "workspaces.config_readme.failed", "config_version_id": config_version_id},
        )
        return item
    fields = config_readme.readme_fields(found)
    names = {f"#f{index}": name for index, name in enumerate(fields)}
    values = {f":f{index}": value for index, value in enumerate(fields.values())}
    repositories.config_versions(resolved).update(
        {"config_version_id": config_version_id},
        update_expression="SET " + ", ".join(f"#f{index} = :f{index}" for index in range(len(fields))),
        expression_names=names,
        expression_values=values,
        condition=Attr("config_version_id").exists(),
    )
    return {**item, **fields}


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
