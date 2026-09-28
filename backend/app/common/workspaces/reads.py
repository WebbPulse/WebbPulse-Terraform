"""Reading a workspace, its variables and its config versions.

Both deployed functions land here. The workspaces domain reads these rows to
serve its own routes and writes them through its own service; the runs domain
reads them to validate a create and to build the runner bundle, under a role
whose grant on these three tables is read only.

Every function is a read. `reconcile_config_version` decides that a `pending` row
whose object is in the bucket now reads `uploaded`, and returns that row, but it
never writes the flip back: persisting it is a workspaces-domain write, and the
workspaces service passes its own writer in when it wants it.
"""

from __future__ import annotations

from typing import Any, Callable, Final

from boto3.dynamodb.conditions import Key

from ..composition.settings import Settings, get_settings
from ..core import variable_cipher
from ..db import repositories

WORKSPACE_ID_PREFIX: Final = "ws-"
CONFIG_VERSION_ID_PREFIX: Final = "cv-"


class WorkspaceNotFound(Exception):
    """No workspace with this id."""


class ConfigVersionNotFound(Exception):
    """No config version with this id, or it belongs to another workspace."""


class VariableNotFound(Exception):
    """No variable with this key on this workspace."""


class RunRoleMissing(Exception):
    """The workspace carries no run role, so there is nothing to assume."""


def config_key(workspace_id: str, config_version_id: str) -> str:
    """The artifacts bucket key one config tarball occupies.

    The contract fixes this layout, and the runner's presigned GET is minted from
    the same function, so the two cannot drift.
    """
    return f"configs/{workspace_id}/{config_version_id}.tar.gz"


def get_workspace(workspace_id: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """One workspace by id, or `WorkspaceNotFound`."""
    resolved = settings or get_settings()
    item = repositories.workspaces(resolved).get({"workspace_id": workspace_id})
    if item is None:
        raise WorkspaceNotFound(workspace_id)
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


def resolved_variables(
    workspace_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, dict[str, str]]:
    """Every variable on one workspace with its plaintext value, split by category.

    The one place a sealed value is opened, and it feeds the run bundle alone. The
    return shape is `{"terraform": {...}, "env": {...}, "hcl": {...}}`. The first
    two are what the runner needs to build its variable file and its process
    environment; `hcl` holds the terraform variables whose value is an HCL
    expression rather than a literal, under the same keys, so the runner can put
    each one in the file the engine parses it from.

    A key never appears in both `terraform` and `hcl`: an HCL variable is routed
    to `hcl` alone, so a runner that ignored the new bucket would drop it rather
    than pass the raw expression off as a literal string.

    A row written before the flag existed carries no `hcl` attribute and reads as
    a literal, which is what it has always been.
    """
    resolved = settings or get_settings()
    out: dict[str, dict[str, str]] = {"terraform": {}, "env": {}, "hcl": {}}
    for item in list_variables(workspace_id, settings=resolved):
        key = str(item["key"])
        category = str(item.get("category", "terraform"))
        if bool(item.get("sensitive", False)):
            value = variable_cipher.open_sealed(item, workspace_id=workspace_id, key=key, settings=resolved)
        else:
            value = str(item.get("value", ""))
        if category == "terraform" and bool(item.get("hcl", False)):
            out["hcl"][key] = value
            continue
        out.setdefault(category, {})[key] = value
    return out


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
    persist: Callable[[str], None] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Move a `pending` row to `uploaded` once its object is in the bucket.

    S3 tells the control plane nothing when a presigned PUT completes, so the row
    a client uploaded against stays `pending` until something looks. Every read
    looks, which is what makes the status a caller sees reflect the bucket rather
    than the moment the URL was minted.

    A row already `uploaded` is returned untouched, so the HEAD is spent only on
    rows that could still change.

    Nothing here writes. With no `persist` the bucket is still consulted and the
    returned row still reads `uploaded`, which is what the runs function needs:
    its role holds a read only grant on this table by design. The workspaces
    domain owns the write and hands in its own writer, which is called with the
    config version id once the object is found.
    """
    if str(item.get("status", "")) != "pending":
        return item

    resolved = settings or get_settings()
    key = str(item.get("key", ""))
    if not key or not config_object_exists(key, settings=resolved):
        return item

    if persist is not None:
        persist(str(item["config_version_id"]))
    return dict(item) | {"status": "uploaded"}


def get_config_version(
    workspace_id: str,
    config_version_id: str,
    *,
    persist: Callable[[str], None] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One config version, or `ConfigVersionNotFound`.

    A row belonging to another workspace reads as absent rather than as a 403, so
    nothing here confirms that an id a caller guessed exists elsewhere.

    `persist` is handed to `reconcile_config_version`: a caller reading these rows
    under a read only grant, as the runs function does, passes none and gets the
    bucket's truth without the write.
    """
    resolved = settings or get_settings()
    item = repositories.config_versions(resolved).get({"config_version_id": config_version_id})
    if item is None or str(item.get("workspace_id")) != workspace_id:
        raise ConfigVersionNotFound(config_version_id)
    return reconcile_config_version(item, persist=persist, settings=resolved)
