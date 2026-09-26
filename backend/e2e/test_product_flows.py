"""The control plane's own journeys against the deployed stage.

The shared suite probes every operation in isolation. These flows prove the sequences
that matter instead: a workspace created with a run role, a config version uploaded to
the presigned PUT, a plan that reaches `planned`, a confirm that reaches `applied`, a
destroy run that removes what the apply created, and the discard and cancel paths that
end a run without applying it.

Every workspace is registered with `created_resources` before it is used, so a flow that
fails part way still hands the cleanup hook something to delete.

A wait and the assertion that follows it are deliberately separate. `_wait_for` stops on
the failure statuses as well as the successful ones, because a run that errors would
otherwise only ever report as a timeout, so every case that then presumes the run
succeeded narrows the result through `_require_status`. Without that a failed plan is
reported as whatever later step it broke, which is how a plan failure once surfaced as a
confusing 409 from the discard several steps downstream.
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

FAILURE_TERMINAL = ("errored", "cancelled", "discarded")
"""The terminal statuses that mean the run did not do what was asked of it.

Kept separate from the success statuses so that a wait and the assertion after it
cannot quietly disagree. `_wait_for` has to be given somewhere to stop on failure,
or a broken run would only ever report as a timeout, but a case that presumes
success must then say so with `_require_status` rather than carrying on.
"""

PLAN_SUCCESS = ("planned", "planned_and_finished", "awaiting_confirmation")
"""The statuses a plan that actually produced a plan settles on.

`awaiting_confirmation` is terminal for the plan phase alone: the run is parked on
its confirmation task token and goes no further until it is confirmed or discarded.
"""

APPLY_SUCCESS = ("applied",)
"""The one status an apply that ran to completion settles on."""

PLAN_TERMINAL = PLAN_SUCCESS + FAILURE_TERMINAL
APPLY_TERMINAL = APPLY_SUCCESS + FAILURE_TERMINAL
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
    pytest.fail(f"run {run_id} was still {last.get('status')!r} after {timeout}s: {_describe(last)}")


def _describe(run: dict[str, Any]) -> str:
    """One line naming a run, its status and whatever diagnostic it carries.

    Read straight off the run body rather than fetched again, so the description is
    of the state the assertion actually saw. `error` is the phase failure text the
    runner reported and is the field that says why a plan or an apply failed.
    """
    parts = [
        f"run={run.get('run_id')}",
        f"status={run.get('status')!r}",
        f"error={run.get('error') or 'none'!r}",
    ]
    if run.get("changes"):
        parts.append(f"changes={run.get('changes')}")
    if run.get("finished_at"):
        parts.append(f"finished_at={run.get('finished_at')}")
    return " ".join(parts)


def _require_delete_refused(api: Any, workspace_id: str, error_code: str, *, force: bool) -> None:
    """Fail unless a workspace delete is a 409 carrying `error_code`, and the workspace survives it."""
    response = api.delete(f"/api/v1/workspaces/{workspace_id}", params={"force": "true"} if force else None)
    body = response.json() if response.status_code == 409 else {}
    assert response.status_code == 409 and body.get("error_code") == error_code, (
        f"a {'force' if force else 'safe'} delete answered {response.status_code}, expected {error_code}: "
        f"{response.text[:400]}"
    )
    assert api.get(f"/api/v1/workspaces/{workspace_id}").status_code == 200, "a refused delete removed the workspace"


def _require_status(run: dict[str, Any], expected: tuple[str, ...], phase: str) -> dict[str, Any]:
    """Fail unless the run settled on one of `expected`, naming the phase that failed.

    This is what keeps a wait honest. `_wait_for` has to stop on the failure statuses
    too, or a run that errors would only ever report as a timeout, so every caller that
    goes on to presume success has to narrow the result here. The message leads with the
    phase, so CI output distinguishes a plan that failed from the later step that was
    never going to work once it had.
    """
    status = run.get("status")
    if status in expected:
        return run
    pytest.fail(f"the {phase} did not succeed, expected one of {expected}: {_describe(run)}")


def _create_run(
    api: Any,
    workspace_id: str,
    config_version_id: str,
    *,
    plan_only: bool,
    is_destroy: bool = False,
) -> str:
    """Start a run, a destroy run when `is_destroy`, and return its id."""
    response = api.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace_id,
            "config_version_id": config_version_id,
            "plan_only": plan_only,
            "is_destroy": is_destroy,
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
        """A run plans, confirms and applies, then a destroy run removes what it applied.

        The destroy is part of the case rather than of cleanup, so the workspace is left
        managing no resources and a failed destroy fails the case instead of leaving a
        `random_pet` behind unnoticed.
        """
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=False)

        planned = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        _require_status(planned, ("planned", "awaiting_confirmation"), "plan")

        _require_delete_refused(api, workspace_id, "WORKSPACE_HAS_ACTIVE_RUN", force=True)

        logs = api.get(f"/api/v1/runs/{run_id}/logs", params={"phase": "plan"})
        assert logs.status_code == 200, logs.text[:400]
        assert isinstance(logs.json()["events"], list)

        confirmed = api.post(f"/api/v1/runs/{run_id}/confirm")
        assert confirmed.status_code in (200, 202), confirmed.text[:400]

        applied = _wait_for(api, run_id, APPLY_TERMINAL, APPLY_TIMEOUT_SECONDS)
        _require_status(applied, APPLY_SUCCESS, "apply")

        apply_logs = api.get(f"/api/v1/runs/{run_id}/logs", params={"phase": "apply"})
        assert apply_logs.status_code == 200, apply_logs.text[:400]

        _require_delete_refused(api, workspace_id, "WORKSPACE_MANAGES_RESOURCES", force=False)

        destroy_id = _create_run(api, workspace_id, config_version_id, plan_only=False, is_destroy=True)
        destroy_planned = _wait_for(api, destroy_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        _require_status(destroy_planned, ("planned", "awaiting_confirmation"), "destroy plan")
        assert destroy_planned["is_destroy"] is True, _describe(destroy_planned)
        assert (destroy_planned.get("changes") or {}).get("destroy", 0) >= 1, _describe(destroy_planned)

        destroy_confirmed = api.post(f"/api/v1/runs/{destroy_id}/confirm")
        assert destroy_confirmed.status_code in (200, 202), destroy_confirmed.text[:400]

        destroyed = _wait_for(api, destroy_id, APPLY_TERMINAL, APPLY_TIMEOUT_SECONDS)
        _require_status(destroyed, APPLY_SUCCESS, "destroy apply")

        deleted = api.delete(f"/api/v1/workspaces/{workspace_id}")
        assert deleted.status_code == 204, (
            f"the safe delete after the destroy answered {deleted.status_code}: {deleted.text[:400]}"
        )
        for gone in (run_id, destroy_id):
            assert api.get(f"/api/v1/runs/{gone}").status_code == 404, f"run {gone} outlived its workspace"

    def test_plan_only_run_finishes_without_applying(self, api: Any, workspace: dict[str, Any]) -> None:
        """A plan-only run reaches a terminal planned status and never applies."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=True)

        finished = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        _require_status(finished, ("planned", "planned_and_finished"), "plan-only run")

    def test_discard_ends_a_planned_run(self, api: Any, workspace: dict[str, Any]) -> None:
        """A run waiting on a confirmation can be discarded instead of applied."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=False)

        planned = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        _require_status(planned, ("planned", "awaiting_confirmation"), "plan")

        discarded = api.post(f"/api/v1/runs/{run_id}/discard")
        assert discarded.status_code in (200, 202), (
            f"the discard answered {discarded.status_code}: {discarded.text[:400]} ({_describe(planned)})"
        )

        final = _wait_for(api, run_id, APPLY_TERMINAL, PLAN_TIMEOUT_SECONDS)
        _require_status(final, ("discarded",), "discard")

    def test_cancel_ends_a_running_run(self, api: Any, workspace: dict[str, Any]) -> None:
        """A run can be cancelled while it is still working."""
        workspace_id = workspace["workspace_id"]
        config_version_id = _upload(api, workspace_id)
        run_id = _create_run(api, workspace_id, config_version_id, plan_only=True)

        cancelled = api.post(f"/api/v1/runs/{run_id}/cancel")
        if cancelled.status_code not in (200, 202):
            pytest.skip(f"the run was no longer cancellable: {cancelled.status_code}")

        final = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
        _require_status(final, ("cancelled", "planned", "planned_and_finished"), "cancel")

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

        _wait_for(api, run_id, RUN_TERMINAL, PLAN_TIMEOUT_SECONDS)
