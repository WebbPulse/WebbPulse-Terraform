"""The organization, entitlement and workspace resources of the `tfe.v2` surface.

The plane has one tenant, so there is one organization, `WebbPulse`. A `cloud {}`
block naming any other answers 404, which the CLI reports as an unknown organization.
Workspace attributes are the stored row rendered under HCP's names, and `permissions`
are the caller's scopes rendered as the booleans the cloud backend checks before it
plans, applies or touches state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from ..core.auth import (
    RUNS_APPLY,
    RUNS_WRITE,
    STATE_DOWNLOAD,
    STATE_WRITE,
    VARIABLES_READ,
    VARIABLES_WRITE,
    WORKSPACES_READ,
    WORKSPACES_WRITE,
)
from .jsonapi import linkage, not_found, resource, timestamp

ORGANIZATION: Final = "WebbPulse"
"""The one organization name a `cloud {}` block may name."""

API_VERSION: Final = "2.6"
"""The `TFP-API-Version` ping answers with; the cloud backend needs 2.5 or later."""

PERMISSION_SCOPES: Final[Mapping[str, tuple[str, ...]]] = {
    "can-destroy": (WORKSPACES_WRITE,),
    "can-force-unlock": (STATE_WRITE,),
    "can-lock": (STATE_WRITE,),
    "can-queue-apply": (RUNS_WRITE, RUNS_APPLY),
    "can-queue-destroy": (RUNS_WRITE, RUNS_APPLY),
    "can-queue-run": (RUNS_WRITE,),
    "can-read-settings": (WORKSPACES_READ,),
    "can-read-state-versions": (STATE_DOWNLOAD,),
    "can-read-variable": (VARIABLES_READ,),
    "can-unlock": (STATE_WRITE,),
    "can-update": (WORKSPACES_WRITE,),
    "can-update-variable": (VARIABLES_WRITE,),
}
"""Each workspace permission and the scopes that grant it, all of them required.

`can-force-delete` is left out on purpose: its presence switches the CLI's
`workspace delete` to HCP's safe delete endpoint."""


def require_organization(organization: str) -> None:
    """Refuse any organization but `WebbPulse` with the 404 go-tfe expects."""
    if organization != ORGANIZATION:
        raise not_found("organization")


def permissions(held: Iterable[str]) -> dict[str, bool]:
    """The workspace `permissions` block for a caller holding `held`."""
    scopes = set(held)
    return {name: all(scope in scopes for scope in needed) for name, needed in PERMISSION_SCOPES.items()}


def organization_resource() -> dict[str, Any]:
    """The organization as a related resource or a document's data."""
    return resource(
        "organizations",
        ORGANIZATION,
        {"name": ORGANIZATION, "external-id": ORGANIZATION},
        links={"self": f"/api/v2/organizations/{ORGANIZATION}"},
    )


def entitlement_set() -> dict[str, Any]:
    """The entitlements the cloud backend reads before anything else.

    `operations` true is what keeps runs remote; false would make the CLI run plans
    locally against remote state.
    """
    return resource(
        "entitlement-sets",
        f"org-{ORGANIZATION}",
        {
            "agents": False,
            "audit-logging": False,
            "configuration-designer": False,
            "cost-estimation": False,
            "global-run-tasks": False,
            "module-tests-generation": False,
            "operations": True,
            "private-module-registry": True,
            "private-run-tasks": False,
            "run-tasks": False,
            "sentinel": False,
            "sso": False,
            "state-storage": True,
            "teams": False,
            "vcs-integrations": True,
            "waypoint-actions": False,
            "waypoint-templates-and-addons": False,
        },
    )


def _vcs_repo(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `vcs-repo` block for a workspace tracking a repository, or None.

    The cloud backend refuses CLI driven applies on a workspace that has one, as HCP
    does, so a VCS workspace stays plan only from the CLI.
    """
    repository = item.get("vcs_repo")
    if not repository:
        return None
    return {
        "branch": str(item.get("tracked_branch") or ""),
        "display-identifier": str(repository),
        "identifier": str(repository),
        "ingress-submodules": False,
        "repository-http-url": f"https://github.com/{repository}",
        "service-provider": "github_app",
        "tags-regex": None,
    }


def workspace_resource(
    item: Mapping[str, Any],
    held: Iterable[str],
    *,
    current_run_id: str | None = None,
    current_state_version_id: str | None = None,
    locked: bool = False,
    resource_count: int = 0,
) -> dict[str, Any]:
    """One stored workspace row as the HCP `workspaces` resource go-tfe decodes."""
    workspace_id = str(item["workspace_id"])
    name = str(item["name"])
    created = timestamp(item.get("created_at"))
    attributes = {
        "actions": {"is-destroyable": True},
        "allow-destroy-plan": True,
        "auto-apply": bool(item.get("auto_apply", False)),
        "auto-apply-run-trigger": False,
        "created-at": created,
        "description": str(item.get("description") or ""),
        "execution-mode": "remote",
        "file-triggers-enabled": bool(item.get("file_triggers_enabled", True)),
        "global-remote-state": False,
        "locked": locked,
        "name": name,
        "operations": True,
        "permissions": permissions(held),
        "queue-all-runs": False,
        "resource-count": resource_count,
        "source": "tfe-api",
        "speculative-enabled": bool(item.get("speculative_plans", True)),
        "structured-run-output-enabled": False,
        "tag-names": [],
        "terraform-version": str(item.get("engine_version") or ""),
        "trigger-patterns": list(item.get("trigger_patterns") or []),
        "trigger-prefixes": [],
        "updated-at": timestamp(item.get("updated_at")) or created,
        "vcs-repo": _vcs_repo(item),
        "working-directory": str(item.get("working_directory") or ""),
    }
    relationships = {
        "current-run": linkage("runs", current_run_id),
        "current-state-version": linkage("state-versions", current_state_version_id),
        "organization": linkage("organizations", ORGANIZATION),
    }
    return resource(
        "workspaces",
        workspace_id,
        attributes,
        relationships=relationships,
        links={
            "self": f"/api/v2/organizations/{ORGANIZATION}/workspaces/{name}",
            "self-html": f"/workspaces/{workspace_id}",
        },
    )


def variable_id(workspace_id: str, key: str) -> str:
    """A stable `var-` id for a stored variable, which has no id of its own."""
    return f"var-{workspace_id.removeprefix('ws-')}-{key}"


def variable_resource(rendered: Mapping[str, Any]) -> dict[str, Any]:
    """One rendered variable as the HCP `vars` resource, a sensitive value null."""
    workspace_id = str(rendered["workspace_id"])
    return resource(
        "vars",
        variable_id(workspace_id, str(rendered["key"])),
        {
            "key": rendered["key"],
            "value": rendered["value"],
            "description": rendered.get("description") or "",
            "category": rendered.get("category") or "terraform",
            "hcl": bool(rendered.get("hcl", False)),
            "sensitive": bool(rendered.get("sensitive", False)),
            "version-id": rendered.get("updated_at") or rendered.get("created_at") or "",
        },
        relationships={"configurable": linkage("workspaces", workspace_id)},
    )


CONFIG_ERROR_MESSAGES: Final[Mapping[str, str]] = {
    "too_large": "The configuration upload is larger than the 250 MB limit, so it was discarded.",
}
"""Each stored `upload_error` and the message the CLI shows for it."""


def configuration_version_resource(item: Mapping[str, Any], *, upload_url: str | None = None) -> dict[str, Any]:
    """One stored config version as HCP's `configuration-versions` resource.

    The row's two statuses are HCP's `pending` and `uploaded`; a row the reconcile
    refused carries `upload_error` and renders `errored` with its message, which is
    what stops the cloud backend's upload poll. `upload-url` is only on the create
    answer, since the presigned PUT is never stored.
    """
    error = item.get("upload_error")
    status = "errored" if error else str(item.get("status") or "pending")
    attributes: dict[str, Any] = {
        "auto-queue-runs": bool(item.get("auto_queue_runs", False)),
        "error": str(error) if error else None,
        "error-message": CONFIG_ERROR_MESSAGES.get(str(error), str(error)) if error else None,
        "source": "github" if item.get("source") == "vcs" else "tfe-api",
        "speculative": bool(item.get("speculative", False)),
        "provisional": bool(item.get("provisional", False)),
        "status": status,
        "upload-url": upload_url,
    }
    config_version_id = str(item["config_version_id"])
    return resource(
        "configuration-versions",
        config_version_id,
        attributes,
        relationships={"ingress-attributes": linkage("ingress-attributes", None)},
        links={"self": f"/api/v2/configuration-versions/{config_version_id}"},
    )
