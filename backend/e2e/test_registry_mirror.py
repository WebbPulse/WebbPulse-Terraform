"""A run's `init` resolves `app.terraform.io` module sources from this plane.

Consumer configurations still name `app.terraform.io/WebbPulse/...` module sources.
The runner writes a CLI config whose `host "app.terraform.io"` block points
`modules.v1` at this plane and hands the engine the run's own registry key as that
host's token, so no HCP credential is involved. The configuration here holds nothing
but the durable staging fixture `WebbPulse/registry-proof/null` 0.1.0, named through
`app.terraform.io`, so a plan-only run that plans proves `init` fetched it from the
plane. Staging only, since only staging carries the fixture, and nothing billable is
created.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
from typing import Any

import httpx
import pytest
from test_product_flows import PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS, POLL_SECONDS

SOURCE = "app.terraform.io/WebbPulse/registry-proof/null"
VERSION = "0.1.0"
CONFIGURATION = f'module "proof" {{\n  source  = "{SOURCE}"\n  version = "{VERSION}"\n}}\n'
LOG_TIMEOUT_SECONDS = 90

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="the registry fixture module is published on staging only",
    ),
]


def _tarball() -> bytes:
    """The one-module configuration as a gzipped tar, built in memory."""
    buffer = io.BytesIO()
    content = CONFIGURATION.encode()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("main.tf")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
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


def _plan(api: Any, workspace_id: str, config_version_id: str) -> dict[str, Any]:
    """Start a plan-only run and return it once it settles, failing on the timeout."""
    response = api.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace_id,
            "config_version_id": config_version_id,
            "plan_only": True,
            "message": "e2e registry mirror",
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


def test_init_resolves_an_app_terraform_io_module_from_the_plane(api: Any, workspace: dict[str, Any]) -> None:
    """A plan-only run over an `app.terraform.io` module source plans on the staging runner."""
    workspace_id = str(workspace["workspace_id"])
    config_version_id = _upload(api, workspace_id)
    run = _plan(api, workspace_id, config_version_id)
    run_id = str(run.get("run_id"))
    print(f"registry mirror proof run={run_id} status={run.get('status')!r}")
    assert run.get("status") in ("planned", "planned_and_finished"), (
        f"init did not resolve {SOURCE} from the plane: run={run_id} status={run.get('status')!r} "
        f"error={run.get('error') or 'none'!r}"
    )
    assert _plan_log_mentions(api, run_id, "registry-proof"), f"run {run_id}'s plan log never names the module"
