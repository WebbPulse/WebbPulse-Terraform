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

RUN_ROLE_CHECK_DURATION_SECONDS: Final = 900
"""The shortest session STS will mint. The check only calls GetCallerIdentity, so
nothing needs the hour a run takes."""

RUN_ROLE_CHECK_SESSION_NAME: Final = "webbpulse-run-role-check"
"""The session name the check assumes under, so a CloudTrail reader can tell a
connection check from a run."""

IAM_ROLE_NAME_MAX_LENGTH: Final = 64
"""The IAM ceiling on a role name. The derived name has to fit inside it."""

RUN_ROLE_ACCESS_DENIED_MESSAGE: Final = "The role does not trust the runner or the external id does not match"
"""What an AccessDenied means in practice, since STS will not say which half failed."""


class WorkspaceNotFound(Exception):
    """No workspace with this id."""


class WorkspaceNameTaken(Exception):
    """Another workspace already holds this name."""


class ConfigVersionNotFound(Exception):
    """No config version with this id, or it belongs to another workspace."""


class VariableNotFound(Exception):
    """No variable with this key on this workspace."""


class RunRoleMissing(Exception):
    """The workspace carries no run role, so there is nothing to assume."""


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


def _sts(settings: Settings) -> Any:
    """An STS client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client("sts", region_name=settings.AWS_REGION_NAME or None)


def check_run_role(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Assume the workspace's run role and report whether it answered.

    Assumes with the workspace id as the external id, the way the runner does, then
    calls GetCallerIdentity on the temporary credentials so the answer names the
    account the role actually lives in rather than the one its ARN claims. The
    outcome is stamped on the row: the timestamp and the account on a success, both
    cleared on a failure, so a stale success cannot outlive a broken trust policy.

    Neither the credentials nor the STS message reach the return value or the log.

    Raises:
        WorkspaceNotFound: No such workspace.
        RunRoleMissing: The workspace carries no run role ARN.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    resolved = settings or get_settings()
    workspace = get_workspace(workspace_id, settings=resolved)
    role_arn = str(workspace.get("run_role_arn", "") or "")
    if not role_arn:
        raise RunRoleMissing(workspace_id)

    try:
        assumed = _sts(resolved).assume_role(
            RoleArn=role_arn,
            RoleSessionName=RUN_ROLE_CHECK_SESSION_NAME,
            ExternalId=workspace_id,
            DurationSeconds=RUN_ROLE_CHECK_DURATION_SECONDS,
        )
        credentials = assumed["Credentials"]
        import boto3

        identity = boto3.client(
            "sts",
            region_name=resolved.AWS_REGION_NAME or None,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        ).get_caller_identity()
    except ClientError as error:
        _record_run_role_check(workspace_id, None, settings=resolved)
        return {"connected": False, "account_id": None, "error": _run_role_error(error)}
    except BotoCoreError:
        _record_run_role_check(workspace_id, None, settings=resolved)
        return {
            "connected": False,
            "account_id": None,
            "error": "The role could not be reached.",
        }

    account_id = str(identity.get("Account", "") or "")
    _record_run_role_check(workspace_id, account_id, settings=resolved)
    return {"connected": True, "account_id": account_id, "error": None}


def _run_role_error(error: Any) -> str:
    """One sentence for a person, from the STS error code alone.

    The code is read rather than the message, because a message can echo the ARN
    and the session name back at a caller who supplied neither.
    """
    code = str(error.response.get("Error", {}).get("Code", "") or "")
    if code in {"AccessDenied", "AccessDeniedException"}:
        return RUN_ROLE_ACCESS_DENIED_MESSAGE
    if code in {"NoSuchEntity", "ValidationError", "InvalidParameterValue"}:
        return "No role with that ARN exists."
    if code == "ExpiredToken":
        return "The control plane's own credentials expired."
    return "The role could not be assumed."


def _record_run_role_check(
    workspace_id: str,
    account_id: str | None,
    *,
    settings: Settings | None = None,
) -> None:
    """Stamp or clear the run role check fields on one workspace row."""
    resolved = settings or get_settings()
    repository = repositories.workspaces(resolved)
    if account_id:
        repository.update(
            {"workspace_id": workspace_id},
            update_expression=("SET run_role_checked_at = :checked, run_role_account_id = :account"),
            expression_values={":checked": now_iso(), ":account": account_id},
            condition=Attr("workspace_id").exists(),
        )
        return
    repository.update(
        {"workspace_id": workspace_id},
        update_expression="REMOVE run_role_checked_at, run_role_account_id",
        condition=Attr("workspace_id").exists(),
    )


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
        "run_role_arn": payload.get("run_role_arn") or None,
        "working_directory": payload.get("working_directory", "") or "",
        "description": payload.get("description", "") or "",
        "created_at": now_iso(),
    }
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
    """Apply a partial edit to one workspace, or `WorkspaceNotFound`.

    A change to `run_role_arn` drops the recorded check outcome in the same write:
    the previous success belonged to the previous role, and leaving it behind would
    show a new, unchecked role as connected.
    """
    resolved = settings or get_settings()
    applied = {key: value for key, value in changes.items() if value is not None}
    if not applied:
        return get_workspace(workspace_id, settings=resolved)

    existing = get_workspace(workspace_id, settings=resolved)
    role_changed = "run_role_arn" in applied and applied["run_role_arn"] != existing.get("run_role_arn")

    applied["updated_at"] = now_iso()
    names = {f"#{key}": key for key in applied}
    values = {f":{key}": value for key, value in applied.items()}
    expression = "SET " + ", ".join(f"#{key} = :{key}" for key in applied)
    if role_changed:
        expression += " REMOVE run_role_checked_at, run_role_account_id"
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


def _s3(settings: Settings) -> Any:
    """An S3 client. Imported late so nothing connects at import."""
    import boto3

    return boto3.client(
        "s3",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
    )


def config_object_exists(key: str, *, settings: Settings) -> bool:
    """Whether the config tarball is in the artifacts bucket.

    A HEAD rather than a GET, so deciding that a multi-megabyte tarball arrived
    costs one metadata call. Any error other than an absent object propagates:
    a denied HEAD is a broken deployment, and swallowing it would report every
    uploaded config version as still pending.
    """
    from botocore.exceptions import ClientError

    try:
        _s3(settings).head_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key)
    except ClientError as error:
        status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        if status == 404 or error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def reconcile_config_version(
    item: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Move a `pending` row to `uploaded` once its object is in the bucket.

    S3 tells the control plane nothing when a presigned PUT completes, so the row
    a client uploaded against stays `pending` until something looks. Every read
    looks, which is what makes the status a caller sees reflect the bucket rather
    than the moment the URL was minted.

    A row already `uploaded` is returned untouched, so the HEAD is spent only on
    rows that could still change.
    """
    if str(item.get("status", "")) != "pending":
        return item

    resolved = settings or get_settings()
    key = str(item.get("key", ""))
    if not key or not config_object_exists(key, settings=resolved):
        return item

    mark_config_version_uploaded(str(item["config_version_id"]), settings=resolved)
    return dict(item) | {"status": "uploaded"}


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
    return reconcile_config_version(item, settings=resolved)


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
