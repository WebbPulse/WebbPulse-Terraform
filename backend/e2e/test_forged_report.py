"""A hostile plan cannot fake its own run report: the check shows the real outcome, not the forged one.

The configuration's `external` data source runs `forge.py` inside the plan, where it
sees exactly what a provider does. It tries to read the runner's environment out of
every process, reach the task credential endpoint and post a forged `phase-result`
claiming no changes, while the real plan adds one `terraform_data` resource. The engine
runs as its own unprivileged user, so it can reach none of that. The proof is the run's
settled outcome: one add, never the forged zero. No secret ever leaves the plan.
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_product_flows import PLAN_SUCCESS, PLAN_TIMEOUT_SECONDS, POLL_SECONDS

CONFIGURATION = Path(__file__).resolve().parent / "forged_report"


def _tarball() -> bytes:
    """The forging configuration as a gzipped tar, built in memory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("main.tf", "forge.py"):
            archive.add(CONFIGURATION / name, arcname=name)
    return buffer.getvalue()


def _upload(api: Any, workspace_id: str) -> str:
    """Create a config version, PUT the configuration to it and return the config version id."""
    payload = _tarball()
    response = api.post(f"/api/v1/workspaces/{workspace_id}/config-versions", json={"size_bytes": len(payload)})
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the config version answered {response.status_code}: {response.text[:400]}")
    body = response.json()
    put = httpx.put(body["upload_url"], content=payload, headers=body["headers"], timeout=60)
    assert put.status_code in (200, 204), f"the presigned PUT answered {put.status_code}: {put.text[:400]}"
    return str(body["config_version"]["config_version_id"])


def _set(api: Any, workspace_id: str, key: str, value: str) -> None:
    """Write one plain terraform workspace variable."""
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
            "message": "e2e forged report",
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
        if run.get("status") in ("planned", "planned_and_finished", "awaiting_confirmation", "errored"):
            return run
        time.sleep(POLL_SECONDS)
    pytest.fail(f"run {run_id} was still {run.get('status')!r} after {PLAN_TIMEOUT_SECONDS}s")


@pytest.mark.e2e_writes
def test_a_plan_cannot_forge_its_run_report(api: Any, e2e_env: Any, workspace: dict[str, Any]) -> None:
    """The plan settles on its real outcome, one add, not the forged no change result the config posts."""
    workspace_id = str(workspace["workspace_id"])
    _set(api, workspace_id, "api_base_url", e2e_env.api_base_url)

    finished = _plan(api, workspace_id, _upload(api, workspace_id))

    assert finished.get("status") in PLAN_SUCCESS, (
        f"the forged report plan did not produce a plan: run={finished.get('run_id')} "
        f"status={finished.get('status')!r} error={finished.get('error') or 'none'!r}"
    )
    changes = finished.get("changes") or {}
    assert int(changes.get("add", 0)) >= 1, (
        "the plan reported no additions, so a forged no change result was accepted: "
        f"run={finished.get('run_id')} changes={changes!r}"
    )
