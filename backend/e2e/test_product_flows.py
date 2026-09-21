"""The control plane's own journeys against the deployed stage.

The shared suite probes every operation in isolation. These flows prove the sequences
that matter instead: a workspace created with a run role, a config version uploaded to
the presigned PUT, a plan that reaches `planned`, a confirm that reaches `applied`, and
the discard and cancel paths that end a run without applying it.

Every workspace is registered with `created_resources` before it is used, so a flow that
fails part way still hands the cleanup hook something to delete.
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "first-run"

PLAN_TERMINAL = ("planned", "planned_and_finished", "errored", "cancelled", "discarded")
APPLY_TERMINAL = ("applied", "errored", "cancelled", "discarded")
RUN_TERMINAL = ("applied", "planned_and_finished", "errored", "cancelled", "discarded")

POLL_SECONDS = 5
PLAN_TIMEOUT_SECONDS = 600
APPLY_TIMEOUT_SECONDS = 900


def _tarball() -> bytes:
    """The `examples/first-run` configuration as a gzipped tar, built in memory."""
    if not EXAMPLE.is_dir():
        pytest.skip(f"{EXAMPLE} is not present, so no configuration can be uploaded")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(EXAMPLE.glob("*.tf")):
            archive.add(path, arcname=path.name)
    return buffer.getvalue()


def _upload(api: Any, workspace_id: str) -> str:
    """Create a config version, PUT the tarball to it and return the config version id.

    The presigned URL signs every header it returns, including `Content-Length`, so the
    declared `size_bytes` has to be the tarball's exact length and the headers have to be
    sent back unchanged.
    """
    payload = _tarball()
    response = api.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": len(payload)},
    )
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the config version answered {response.status_code}: {response.text[:400]}")
    body = response.json()

    put = httpx.put(body["upload_url"], content=payload, headers=body["headers"], timeout=60)
    assert put.status_code in (200, 204), f"the presigned PUT answered {put.status_code}: {put.text[:400]}"

    return str(body["config_version"]["config_version_id"])


def _wait_for(api: Any, run_id: str, terminal: tuple[str, ...], timeout: int) -> dict[str, Any]:
    """Poll one run until its status is terminal, failing on the timeout."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api.get(f"/api/v1/runs/{run_id}")
        assert response.status_code == 200, response.text[:400]
        last = dict(response.json())
        if last.get("status") in terminal:
            return last
        time.sleep(POLL_SECONDS)
    pytest.fail(f"run {run_id} was still {last.get('status')!r} after {timeout}s")


def _create_run(api: Any, workspace_id: str, config_version_id: str, *, plan_only: bool) -> str:
    """Start a run and return its id."""
    response = api.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace_id,
            "config_version_id": config_version_id,
            "plan_only": plan_only,
            "message": "e2e",
        },
    )
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the run answered {response.status_code}: {response.text[:400]}")
    return str(response.json()["run_id"])


@pytest.mark.e2e_writes
class TestWorkspaceLifecycle:
    """A workspace created through the API reads back, lists and deletes."""

    def test_workspace_round_trips(self, api: Any, e2e_env: Any, workspace: dict[str, Any]) -> None:
        """The created workspace reads back by id and appears in the list."""
        workspace_id = workspace["workspace_id"]

        readback = api.get(f"/api/v1/workspaces/{workspace_id}")
        assert readback.status_code == 200, readback.text[:400]
        assert readback.json()["name"].startswith(e2e_env.resource_prefix)

        listed = api.get("/api/v1/workspaces")
        assert listed.status_code == 200, listed.text[:400]
        identifiers = [str(item["workspace_id"]) for item in listed.json()["items"]]
        assert workspace_id in identifiers

    def test_variables_round_trip(self, api: Any, workspace: dict[str, Any]) -> None:
        """A workspace variable writes, reads back and deletes."""
        workspace_id = workspace["workspace_id"]
        path = f"/api/v1/workspaces/{workspace_id}/variables/e2e_marker"

        written = api.put(path, json={"value": "e2e", "sensitive": False})
        assert written.status_code in (200, 201), written.text[:400]

        readback = api.get(path)
        assert readback.status_code == 200, readback.text[:400]
        assert readback.json()["value"] == "e2e"

        deleted = api.delete(path)
        assert deleted.status_code in (200, 204), deleted.text[:400]


@pytest.mark.e2e_writes
class TestConfigVersions:
    """A config version uploaded to the presigned PUT reaches `uploaded`."""

    def test_upload_marks_the_config_version_uploaded(self, api: Any, workspace: dict[str, Any]) -> None:
        """The tarball lands and the config version's status moves off `pending`."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)

        readback = api.get(f"/api/v1/workspaces/{workspace_id}/config-versions/{config_version_id}")
        assert readback.status_code == 200, readback.text[:400]
        assert readback.json()["status"] == "uploaded"

        listed = api.get(f"/api/v1/workspaces/{workspace_id}/config-versions")
        assert listed.status_code == 200, listed.text[:400]
        identifiers = [str(item["config_version_id"]) for item in listed.json()["items"]]
        assert config_version_id in identifiers


@pytest.mark.e2e_writes
class TestRunLifecycle:
    """The run lifecycle end to end: plan, confirm, apply, and the paths that do not apply."""

    def test_plan_and_apply(self, api: Any, workspace: dict[str, Any]) -> None:
        """A run plans, confirms and applies, and its logs are readable."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=False)

        planned = _wait_for(api, run_id, PLAN_TERMINAL + ("awaiting_confirmation",), PLAN_TIMEOUT_SECONDS)
        assert planned["status"] in ("planned", "awaiting_confirmation"), planned

        logs = api.get(f"/api/v1/runs/{run_id}/logs", params={"phase": "plan"})
        assert logs.status_code == 200, logs.text[:400]
        assert isinstance(logs.json()["events"], list)

        confirmed = api.post(f"/api/v1/runs/{run_id}/confirm")
        assert confirmed.status_code in (200, 202), confirmed.text[:400]

        applied = _wait_for(api, run_id, APPLY_TERMINAL, APPLY_TIMEOUT_SECONDS)
        assert applied["status"] == "applied", applied

        apply_logs = api.get(f"/api/v1/runs/{run_id}/logs", params={"phase": "apply"})
        assert apply_logs.status_code == 200, apply_logs.text[:400]

    def test_plan_only_run_finishes_without_applying(self, api: Any, workspace: dict[str, Any]) -> None:
        """A plan-only run reaches a terminal planned status and never applies."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=True)

        finished = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        assert finished["status"] in ("planned", "planned_and_finished"), finished

    def test_discard_ends_a_planned_run(self, api: Any, workspace: dict[str, Any]) -> None:
        """A run waiting on a confirmation can be discarded instead of applied."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=False)

        _wait_for(api, run_id, PLAN_TERMINAL + ("awaiting_confirmation",), PLAN_TIMEOUT_SECONDS)

        discarded = api.post(f"/api/v1/runs/{run_id}/discard")
        assert discarded.status_code in (200, 202), discarded.text[:400]

        final = _wait_for(api, run_id, APPLY_TERMINAL, PLAN_TIMEOUT_SECONDS)
        assert final["status"] == "discarded", final

    def test_cancel_ends_a_running_run(self, api: Any, workspace: dict[str, Any]) -> None:
        """A run can be cancelled while it is still working."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=True)

        cancelled = api.post(f"/api/v1/runs/{run_id}/cancel")
        if cancelled.status_code not in (200, 202):
            pytest.skip(f"the run was no longer cancellable: {cancelled.status_code}")

        final = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        assert final["status"] in ("cancelled", "planned", "planned_and_finished"), final

    def test_runs_list_carries_this_workspace(self, api: Any, workspace: dict[str, Any]) -> None:
        """A run started for this workspace appears in the runs list, and is then ended.

        The list is the assertion, so the run is cancelled straight afterwards rather
        than planned out: the case needs nothing from the plan, and leaving an execution
        running would outlive the test.
        """
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=True)

        listed = api.get("/api/v1/runs", params={"workspace_id": workspace_id})
        assert listed.status_code == 200, listed.text[:400]
        identifiers = [str(item["run_id"]) for item in listed.json()["items"]]
        assert run_id in identifiers

        cancelled = api.post(f"/api/v1/runs/{run_id}/cancel")
        assert cancelled.status_code in (200, 202, 409), cancelled.text[:400]

        final = _wait_for(api, run_id, RUN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        assert final["status"] in RUN_TERMINAL, final
