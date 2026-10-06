"""A saved plan applies against the files its plan generated, as on HCP Terraform.

The configuration's module renders a template into an `archive_file` zip under
`.build/` while it plans, and a root resource hashes that zip. Terraform evaluates
the hash again when the saved plan applies, so the apply fails unless it runs in the
working directory the plan left. The runner archives that directory after the plan
and the apply restores it, which is what this case proves on staging.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from test_product_flows import (
    APPLY_SUCCESS,
    APPLY_TERMINAL,
    APPLY_TIMEOUT_SECONDS,
    PLAN_TERMINAL,
    PLAN_TIMEOUT_SECONDS,
    _create_run,
    _describe,
    _require_status,
    _wait_for,
)

CONFIGURATION = Path(__file__).resolve().parent / "workdir_carry"
FILES = ("main.tf", "bundle/main.tf", "bundle/handler.js.tftpl")


def _tarball() -> bytes:
    """The configuration as a gzipped tar, built in memory without any `.build` output."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in FILES:
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


def _plan_and_apply(
    api: Any, step_up_again: Callable[[], Any], workspace_id: str, config_version_id: str, *, is_destroy: bool
) -> dict[str, Any]:
    """Plan, confirm and apply one run, returning it once applied."""
    run_id = _create_run(api, workspace_id, config_version_id, plan_only=False, is_destroy=is_destroy)
    planned = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
    _require_status(planned, ("planned", "awaiting_confirmation"), "plan")
    confirmed = step_up_again().post(f"/api/v1/runs/{run_id}/confirm")
    assert confirmed.status_code in (200, 202), confirmed.text[:400]
    applied = _wait_for(api, run_id, APPLY_TERMINAL, APPLY_TIMEOUT_SECONDS)
    return _require_status(applied, APPLY_SUCCESS, "destroy apply" if is_destroy else "apply")


@pytest.mark.e2e_writes
def test_an_apply_reads_what_its_plan_generated(
    api: Any, workspace: dict[str, Any], step_up_again: Callable[[], Any]
) -> None:
    """The apply hashes the zip the plan built, then a destroy leaves nothing managed."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _upload(api, workspace_id)

    applied = _plan_and_apply(api, step_up_again, workspace_id, config_version_id, is_destroy=False)
    assert (applied.get("changes") or {}).get("add", 0) == 1, _describe(applied)

    _plan_and_apply(api, step_up_again, workspace_id, config_version_id, is_destroy=True)
