"""A plan may assume the reader roles its workspace lists, and no other role.

A plan's run role session is `ReadOnlyAccess`, which holds no `sts:AssumeRole`, so
a workspace names exact reader roles in `plan_assume_role_arns` and the plan session
gains `sts:AssumeRole` on those alone. The configuration picks its role with the
runner's `TF_VAR_webbpulse_run_phase`, the convention real configurations follow.

Two permissionless roles in the staging account, both trusted by and assumable from
the e2e run role, make the session policy the only difference: the listed one is
assumed and the plan finishes, the unlisted one is refused and the plan errors. The
writer is deliberately a role that does not exist, so a plan that ignored the phase
variable would fail in both cases.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_product_flows import PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS, POLL_SECONDS

CONFIGURATION = Path(__file__).resolve().parent / "plan_assume"

LISTED_VARIABLE = "E2E_PLAN_READER_ROLE_ARN"
UNLISTED_VARIABLE = "E2E_PLAN_UNLISTED_ROLE_ARN"

LOG_TIMEOUT_SECONDS = 90


def _role_arn(variable: str) -> str:
    """One proof role's ARN from the environment, skipping when the stage has none."""
    value = os.environ.get(variable, "").strip()
    if not value:
        pytest.skip(f"{variable} is not set, so the plan assume proof has no roles")
    return value


def _writer_role_arn(reader: str) -> str:
    """A role ARN in the reader's account that does not exist, standing in for the writer."""
    return reader.rsplit("/", 1)[0] + "/webbpulse-e2e-writer-does-not-exist"


def _tarball() -> bytes:
    """The proof configuration as a gzipped tar, built in memory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(CONFIGURATION / "main.tf", arcname="main.tf")
    return buffer.getvalue()


def _upload(api: Any, workspace_id: str) -> str:
    """Create a config version, PUT the configuration to it and return its id."""
    payload = _tarball()
    response = api.post(f"/api/v1/workspaces/{workspace_id}/config-versions", json={"size_bytes": len(payload)})
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the config version answered {response.status_code}: {response.text[:400]}")
    body = response.json()
    put = httpx.put(body["upload_url"], content=payload, headers=body["headers"], timeout=60)
    assert put.status_code in (200, 204), f"the presigned PUT answered {put.status_code}: {put.text[:400]}"
    return str(body["config_version"]["config_version_id"])


def _set(api: Any, workspace_id: str, key: str, value: str) -> None:
    """Write one plain Terraform variable."""
    response = api.put(
        f"/api/v1/workspaces/{workspace_id}/variables/{key}",
        json={"value": value, "category": "terraform", "sensitive": False},
    )
    assert response.status_code in (200, 201), f"setting {key} answered {response.status_code}: {response.text[:400]}"


def _plan(api: Any, workspace_id: str, config_version_id: str) -> dict[str, Any]:
    """Start a plan only run and return it once it settles, failing on the timeout."""
    response = api.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace_id,
            "config_version_id": config_version_id,
            "plan_only": True,
            "message": "e2e",
        },
    )
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the run answered {response.status_code}: {response.text[:400]}")
    run_id = str(response.json()["run_id"])
    deadline = time.monotonic() + PLAN_TIMEOUT_SECONDS
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        polled = api.get(f"/api/v1/runs/{run_id}")
        assert polled.status_code == 200, polled.text[:400]
        run = dict(polled.json())
        if run.get("status") in PLAN_TERMINAL:
            return run
        time.sleep(POLL_SECONDS)
    pytest.fail(f"run {run_id} was still {run.get('status')!r} after {PLAN_TIMEOUT_SECONDS}s")


def _plan_log_mentions(api: Any, run_id: str, needle: str) -> bool:
    """Whether the plan log shows `needle`, polling while the log catches up."""
    deadline = time.monotonic() + LOG_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        logs = api.get(f"/api/v1/runs/{run_id}/logs", params={"phase": "plan"})
        if logs.status_code == 200 and needle in str(logs.json().get("events", [])):
            return True
        time.sleep(POLL_SECONDS)
    return False


def _describe(run: dict[str, Any]) -> str:
    """One line naming a run, its status and its error."""
    return f"run={run.get('run_id')} status={run.get('status')!r} error={run.get('error') or 'none'!r}"


@pytest.mark.e2e_writes
def test_a_plan_assumes_a_listed_reader_and_is_refused_an_unlisted_one(api: Any, workspace: dict[str, Any]) -> None:
    """The listed reader is assumed in plan; the unlisted one, equally trusted, is denied by the session policy."""
    listed = _role_arn(LISTED_VARIABLE)
    unlisted = _role_arn(UNLISTED_VARIABLE)
    workspace_id = str(workspace["workspace_id"])

    patched = api.patch(f"/api/v1/workspaces/{workspace_id}", json={"plan_assume_role_arns": [listed]})
    assert patched.status_code == 200, patched.text[:400]
    assert patched.json()["plan_assume_role_arns"] == [listed]

    config_version_id = _upload(api, workspace_id)
    _set(api, workspace_id, "writer_role_arn", _writer_role_arn(listed))

    _set(api, workspace_id, "reader_role_arn", listed)
    allowed = _plan(api, workspace_id, config_version_id)
    assert allowed.get("status") in ("planned", "planned_and_finished"), (
        f"the plan could not assume the listed reader: {_describe(allowed)}"
    )

    _set(api, workspace_id, "reader_role_arn", unlisted)
    denied = _plan(api, workspace_id, config_version_id)
    assert denied.get("status") == "errored", (
        f"the plan assumed a role its workspace does not list: {_describe(denied)}"
    )
    assert _plan_log_mentions(api, str(denied["run_id"]), "AccessDenied"), (
        f"the unlisted plan errored for a reason other than a denied AssumeRole: {_describe(denied)}"
    )
