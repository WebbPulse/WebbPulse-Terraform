"""Workspace, variable and config version storage.

The uniqueness of a workspace name is enforced by a conditional write against the
`by_name` GSI read, not by the index itself: DynamoDB has no unique index, so the
claim is a condition on the item plus a query that refuses a duplicate. Two
concurrent creates of the same name can both pass the query, so the condition on
`workspace_id` is what keeps the table from holding two rows with one id, and the
query is what makes the ordinary case a clean 409.
"""

from __future__ import annotations

from typing import Any, Final

from boto3.dynamodb.conditions import Attr, Key
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.core import variable_cipher
from ...common.db import repositories
from ...common.db.conditions import condition_failed
from ...common.db.tables import (
    CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
    WORKSPACES_BY_NAME_INDEX,
)

WORKSPACE_ID_PREFIX: Final = "ws-"
CONFIG_VERSION_ID_PREFIX: Final = "cv-"

CONFIG_CONTENT_TYPE: Final = "application/gzip"
"""The one content type a config tarball may declare, signed into the PUT."""

CONFIG_UPLOAD_EXPIRES_IN: Final = 900
"""Fifteen minutes for the client to start its upload, the package's own default."""


class WorkspaceNotFound(Exception):
    """No workspace with this id."""


class WorkspaceNameTaken(Exception):
    """Another workspace already holds this name."""


class ConfigVersionNotFound(Exception):
    """No config version with this id, or it belongs to another workspace."""


class VariableNotFound(Exception):
    """No variable with this key on this workspace."""


def config_key(workspace_id: str, config_version_id: str) -> str:
    """The artifacts bucket key one config tarball occupies.

    The contract fixes this layout, and the runner's presigned GET is minted from
    the same function, so the two cannot drift.
    """
    return f"configs/{workspace_id}/{config_version_id}.tar.gz"


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
        "run_role_arn": payload["run_role_arn"],
        "working_directory": payload.get("working_directory", "") or "",
        "description": payload.get("description", "") or "",
        "created_at": now_iso(),
    }
    try:
        with condition_failed(repository.table_name, key={"workspace_id": item["workspace_id"]}):
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


def get_workspace(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One workspace by id, or `WorkspaceNotFound`."""
    resolved = settings or get_settings()
    item = repositories.workspaces(resolved).get({"workspace_id": workspace_id})
    if item is None:
        raise WorkspaceNotFound(workspace_id)
    return item


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
    """Apply a partial edit to one workspace, or `WorkspaceNotFound`."""
    resolved = settings or get_settings()
    applied = {key: value for key, value in changes.items() if value is not None}
    if not applied:
        return get_workspace(workspace_id, settings=resolved)

    applied["updated_at"] = now_iso()
    names = {f"#{key}": key for key in applied}
    values = {f":{key}": value for key, value in applied.items()}
    expression = "SET " + ", ".join(f"#{key} = :{key}" for key in applied)
    repository = repositories.workspaces(resolved)
    try:
        with condition_failed(repository.table_name, key={"workspace_id": workspace_id}):
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


def delete_workspace(workspace_id: str, *, settings: Settings | None = None) -> None:
    """Delete one workspace and every variable on it, or `WorkspaceNotFound`.

    The config versions and runs are left alone: they carry the history of what
    ran, and the artifacts bucket lifecycle expires their objects at 90 days.
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
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
    """
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    existing = repositories.variables(resolved).get({"workspace_id": workspace_id, "key": key})

    sensitive = bool(payload.get("sensitive", False))
    item: dict[str, Any] = {
        "workspace_id": workspace_id,
        "key": key,
        "category": payload.get("category", "terraform"),
        "sensitive": sensitive,
        "description": payload.get("description", "") or "",
        "created_at": str(existing["created_at"]) if existing else now_iso(),
    }
    if existing:
        item["updated_at"] = now_iso()

    value = str(payload["value"])
    if sensitive:
        item.update(variable_cipher.seal(value, workspace_id=workspace_id, key=key, settings=resolved))
    else:
        item["value"] = value

    repositories.variables(resolved).put(item)
    return item


def get_variable(
    workspace_id: str,
    key: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One stored variable row, or `VariableNotFound`."""
    resolved = settings or get_settings()
    item = repositories.variables(resolved).get({"workspace_id": workspace_id, "key": key})
    if item is None:
        raise VariableNotFound(key)
    return item


def list_variables(workspace_id: str, *, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every stored variable row on one workspace, by key."""
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    return list(repositories.variables(resolved).iter_query(Key("workspace_id").eq(workspace_id)))


def delete_variable(workspace_id: str, key: str, *, settings: Settings | None = None) -> None:
    """Delete one variable, or `VariableNotFound`."""
    resolved = settings or get_settings()
    get_variable(workspace_id, key, settings=resolved)
    repositories.variables(resolved).delete({"workspace_id": workspace_id, "key": key})


def resolved_variables(
    workspace_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, dict[str, str]]:
    """Every variable on one workspace with its plaintext value, split by category.

    The one place a sealed value is opened, and it feeds the run bundle alone. The
    return shape is `{"terraform": {...}, "env": {...}}`, which is what the runner
    needs to build its command line and its process environment.
    """
    resolved = settings or get_settings()
    out: dict[str, dict[str, str]] = {"terraform": {}, "env": {}}
    for item in list_variables(workspace_id, settings=resolved):
        key = str(item["key"])
        category = str(item.get("category", "terraform"))
        if bool(item.get("sensitive", False)):
            value = variable_cipher.open_sealed(item, workspace_id=workspace_id, key=key, settings=resolved)
        else:
            value = str(item.get("value", ""))
        out.setdefault(category, {})[key] = value
    return out


def render_variable(item: dict[str, Any]) -> dict[str, Any]:
    """One stored variable row as the API returns it, with no sealed fields.

    A sensitive variable's `value` is `None` rather than absent, so a client can
    tell "withheld" from "empty string" without reading the `sensitive` flag.
    """
    sensitive = bool(item.get("sensitive", False))
    return {
        "workspace_id": str(item["workspace_id"]),
        "key": str(item["key"]),
        "value": None if sensitive else str(item.get("value", "")),
        "category": str(item.get("category", "terraform")),
        "sensitive": sensitive,
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
    recorded. The row goes to `uploaded` when a run is created against it, which
    is the first moment anything reads the object.
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


def get_config_version(
    workspace_id: str,
    config_version_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One config version, or `ConfigVersionNotFound`.

    A row belonging to another workspace reads as absent rather than as a 403, so
    nothing here confirms that an id a caller guessed exists elsewhere.
    """
    resolved = settings or get_settings()
    item = repositories.config_versions(resolved).get({"config_version_id": config_version_id})
    if item is None or str(item.get("workspace_id")) != workspace_id:
        raise ConfigVersionNotFound(config_version_id)
    return item


def list_config_versions(
    workspace_id: str,
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """One workspace's config versions, oldest first."""
    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    return list(
        repositories.config_versions(resolved).iter_query(
            Key("workspace_id").eq(workspace_id),
            index_name=CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
        )
    )


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
