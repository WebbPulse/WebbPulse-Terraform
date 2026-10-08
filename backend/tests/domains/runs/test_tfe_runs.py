"""The `tfe.v2` runs the cloud backend creates, polls, confirms and streams logs from.

The CLI's plan and apply are a loop over these routes, so each case asserts the
shape and the status go-tfe reads at one step of that loop.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import boto3

from app.common.composition.settings import get_settings
from app.common.core.auth import RUNS_READ, RUNS_WRITE
from app.domains.runs import run_options, tfe_runs
from app.domains.runs import service as runs_service
from app.domains.workspaces import service as workspaces_service
from tests.conftest import ARTIFACTS_BUCKET, REGION
from tests.domains.runs.test_logs import write_events

API = "/api/v2"
JSON_API = "application/vnd.api+json"


def _config_version(auth_client, workspace_id: str, *, speculative: bool) -> str:
    """An uploaded config version created the way the cloud backend creates one."""
    body = {"data": {"type": "configuration-versions", "attributes": {"speculative": speculative}}}
    response = auth_client.post(
        f"{API}/workspaces/{workspace_id}/configuration-versions",
        content=json.dumps(body),
        headers={"Content-Type": JSON_API},
    )
    assert response.status_code == 201, response.text
    config_version_id = response.json()["data"]["id"]
    workspaces_service.mark_config_version_uploaded(config_version_id)
    return config_version_id


def _create(client, workspace_id: str, config_version_id: str, **attributes):
    """POST a run the way go-tfe does."""
    body = {
        "data": {
            "type": "runs",
            "attributes": attributes,
            "relationships": {
                "workspace": {"data": {"type": "workspaces", "id": workspace_id}},
                "configuration-version": {"data": {"type": "configuration-versions", "id": config_version_id}},
            },
        }
    }
    return client.post(f"{API}/runs", content=json.dumps(body), headers={"Content-Type": JSON_API})


def _log_path(url: str) -> str:
    """The path of a signed log URL, which the test client serves."""
    return urlparse(url).path


def test_a_run_on_a_speculative_version_is_plan_only(auth_client, workspace, state_machine):
    """`terraform plan` without `-out` uploads a speculative version and expects a plan only run."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _config_version(auth_client, workspace_id, speculative=True)
    response = _create(auth_client, workspace_id, config_version_id, **{"auto-apply": False})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["type"] == "runs"
    assert data["id"].startswith("run-")
    assert data["attributes"]["plan-only"] is True
    assert data["attributes"]["status"] in ("pending", "planning")
    assert data["relationships"]["plan"]["data"] == {"type": "plans", "id": tfe_runs.plan_id(data["id"])}
    assert data["relationships"]["workspace"]["data"]["id"] == workspace_id


def test_run_options_reach_the_stored_run(auth_client, workspace, state_machine):
    """Targets, replacements, refresh flags and `-var` values are stored, the values sealed."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _config_version(auth_client, workspace_id, speculative=False)
    response = _create(
        auth_client,
        workspace_id,
        config_version_id,
        **{
            "target-addrs": ["terraform_data.a"],
            "replace-addrs": ["terraform_data.b"],
            "refresh": False,
            "variables": [{"key": "name", "value": '"example"'}],
            "is-destroy": True,
        },
    )
    assert response.status_code == 201, response.text
    attributes = response.json()["data"]["attributes"]
    assert attributes["target-addrs"] == ["terraform_data.a"]
    assert attributes["replace-addrs"] == ["terraform_data.b"]
    assert attributes["refresh"] is False
    assert attributes["is-destroy"] is True
    assert attributes["variables"] == [{"key": "name"}]
    row = runs_service.get_run(response.json()["data"]["id"])
    assert run_options.SEALED_ATTRIBUTE in row
    assert "example" not in json.dumps(row, default=str)


def test_an_unparseable_run_variable_is_a_json_api_422(auth_client, workspace, state_machine):
    """A `-var` value that is not one HCL expression is refused before a run exists."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _config_version(auth_client, workspace_id, speculative=False)
    response = _create(auth_client, workspace_id, config_version_id, variables=[{"key": "name", "value": "{"}])
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["status"] == "422"


def test_a_saved_plan_never_applies_on_its_own(auth_client, workspace, state_machine):
    """`terraform plan -out` saves the plan, which waits for `terraform apply <planfile>`."""
    workspace_id = workspace["workspace_id"]
    workspaces_service.update_workspace(workspace_id, {"auto_apply": True})
    config_version_id = _config_version(auth_client, workspace_id, speculative=False)
    response = _create(auth_client, workspace_id, config_version_id, **{"save-plan": True})
    assert response.status_code == 201, response.text
    attributes = response.json()["data"]["attributes"]
    assert attributes["save-plan"] is True
    assert attributes["plan-only"] is False
    assert attributes["auto-apply"] is False
    row = runs_service.get_run(response.json()["data"]["id"])
    assert row["save_plan"] is True
    assert runs_service.auto_apply_eligible(row | {"status": "awaiting_confirmation"}) is False


def test_save_plan_on_a_speculative_version_is_plan_only(auth_client, workspace, state_machine):
    """A speculative version cannot be applied, so it is never saved."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _config_version(auth_client, workspace_id, speculative=True)
    response = _create(auth_client, workspace_id, config_version_id, **{"save-plan": True})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["attributes"]["save-plan"] is False
    assert "save_plan" not in runs_service.get_run(response.json()["data"]["id"])


def test_auto_approve_needs_runs_apply(scoped_client, auth_client, workspace, state_machine):
    """`-auto-approve` is a confirmation made up front, so it needs the apply scope."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _config_version(auth_client, workspace_id, speculative=False)
    client = scoped_client(RUNS_READ, RUNS_WRITE)
    response = _create(client, workspace_id, config_version_id, **{"auto-apply": True})
    assert response.status_code == 422


def test_auto_approve_overrides_the_workspace(auth_client, workspace, state_machine):
    """A run that asks to auto-apply does, whatever the workspace's own setting."""
    workspace_id = workspace["workspace_id"]
    config_version_id = _config_version(auth_client, workspace_id, speculative=False)
    response = _create(auth_client, workspace_id, config_version_id, **{"auto-apply": True})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["attributes"]["auto-apply"] is True
    row = runs_service.get_run(response.json()["data"]["id"])
    assert row[runs_service.AUTO_APPLY_OVERRIDE_ATTRIBUTE] is True
    assert runs_service.auto_apply_eligible(row | {"status": "awaiting_confirmation"}) is True


def test_a_missing_configuration_version_is_a_422(auth_client, workspace):
    """A run names its configuration version, as the CLI always does."""
    body = {
        "data": {
            "type": "runs",
            "relationships": {"workspace": {"data": {"type": "workspaces", "id": workspace["workspace_id"]}}},
        }
    }
    response = auth_client.post(f"{API}/runs", content=json.dumps(body), headers={"Content-Type": JSON_API})
    assert response.status_code == 422


def test_a_plan_waiting_on_its_token_still_reads_running(auth_client, planned_with_changes):
    """Until the token lands the CLI must keep streaming, or it would find nothing to confirm."""
    run_id = planned_with_changes["run_id"]
    body = auth_client.get(f"{API}/runs/{run_id}", params={"include": "plan,workspace"}).json()
    assert body["data"]["attributes"]["status"] == "planning"
    assert body["data"]["attributes"]["actions"]["is-confirmable"] is False
    included = {item["type"]: item for item in body["included"]}
    assert included["plans"]["attributes"]["status"] == "running"
    assert included["plans"]["attributes"]["has-changes"] is True
    assert included["workspaces"]["id"] == planned_with_changes["workspace_id"]


def test_a_confirmable_run_reads_planned(auth_client, awaiting_confirmation):
    """With its token held the run is `planned`, confirmable, and its plan finished."""
    run_id = awaiting_confirmation["run_id"]
    data = auth_client.get(f"{API}/runs/{run_id}").json()["data"]
    assert data["attributes"]["status"] == "planned"
    assert data["attributes"]["actions"]["is-confirmable"] is True
    assert data["attributes"]["actions"]["is-discardable"] is True
    assert data["attributes"]["permissions"]["can-apply"] is True
    plan = auth_client.get(f"{API}/plans/{tfe_runs.plan_id(run_id)}").json()["data"]
    assert plan["attributes"]["status"] == "finished"
    assert plan["attributes"]["resource-additions"] == 2
    assert plan["attributes"]["log-read-url"].startswith("http")
    apply = auth_client.get(f"{API}/applies/{tfe_runs.apply_id(run_id)}").json()["data"]
    assert apply["attributes"]["status"] == "pending"


def test_a_confirmable_saved_plan_reads_planned_and_saved(auth_client, awaiting_confirmation):
    """A saved plan reads as HCP's `planned_and_saved` and is still confirmable by its apply."""
    run_id = awaiting_confirmation["run_id"]
    runs_service._update_run(run_id, {"save_plan": True}, settings=get_settings())
    data = auth_client.get(f"{API}/runs/{run_id}", params={"include": "workspace"}).json()["data"]
    assert data["attributes"]["status"] == "planned_and_saved"
    assert data["attributes"]["save-plan"] is True
    assert data["attributes"]["actions"]["is-confirmable"] is True
    response = auth_client.post(f"{API}/runs/{run_id}/actions/apply", content=b"{}")
    assert response.status_code == 202, response.text
    assert runs_service.get_run(run_id)["status"] == "applying"


def test_stage_includes_read_as_empty(auth_client, awaiting_confirmation):
    """Task stages, policy evaluations, checks and cost estimates are empty, as on a workspace with none."""
    run_id = awaiting_confirmation["run_id"]
    response = auth_client.get(f"{API}/runs/{run_id}", params={"include": "task_stages,tf_policy_evaluations"})
    assert response.status_code == 200, response.text
    relationships = response.json()["data"]["relationships"]
    assert relationships["task-stages"] == {"data": []}
    assert relationships["tf-policy-evaluations"] == {"data": []}
    assert relationships["policy-checks"] == {"data": []}
    assert relationships["cost-estimate"] == {"data": None}


def test_apply_confirms_the_run(auth_client, awaiting_confirmation):
    """Answering `yes` confirms the run, and its apply then reads running."""
    run_id = awaiting_confirmation["run_id"]
    response = auth_client.post(f"{API}/runs/{run_id}/actions/apply", content=b"{}")
    assert response.status_code == 202, response.text
    assert runs_service.get_run(run_id)["status"] == "applying"
    apply = auth_client.get(f"{API}/applies/{tfe_runs.apply_id(run_id)}").json()["data"]
    assert apply["attributes"]["status"] == "running"
    again = auth_client.post(f"{API}/runs/{run_id}/actions/apply", content=b"{}")
    assert again.status_code == 409


def test_discard_ends_the_run(auth_client, awaiting_confirmation):
    """Answering anything else discards the run."""
    run_id = awaiting_confirmation["run_id"]
    response = auth_client.post(f"{API}/runs/{run_id}/actions/discard", content=b"{}")
    assert response.status_code == 202, response.text
    data = auth_client.get(f"{API}/runs/{run_id}").json()["data"]
    assert data["attributes"]["status"] == "discarded"
    apply = auth_client.get(f"{API}/applies/{tfe_runs.apply_id(run_id)}").json()["data"]
    assert apply["attributes"]["status"] == "unreachable"


def test_a_running_plan_log_streams_without_its_end_marker(auth_client, client, runner_log_group, created_run):
    """The log URL reads with no bearer, starts with STX, and holds back ETX while the plan runs."""
    run_id = created_run["run_id"]
    write_events(run_id, "plan", ["Initializing...", " ", "Plan: 1 to add"])
    url = auth_client.get(f"{API}/plans/{tfe_runs.plan_id(run_id)}").json()["data"]["attributes"]["log-read-url"]
    response = client.get(_log_path(url), params={"limit": 65536, "offset": 0})
    assert response.status_code == 200, response.text
    assert response.content == b"\x02Initializing...\n\nPlan: 1 to add\n"
    window = client.get(_log_path(url), params={"limit": 4, "offset": 1})
    assert window.content == b"Init"


def test_a_finished_plan_log_reads_its_transcript_and_ends(auth_client, client, awaiting_confirmation):
    """A done phase reads the uploaded transcript and ends with ETX."""
    run_id = awaiting_confirmation["run_id"]
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=ARTIFACTS_BUCKET, Key=runs_service.log_key(run_id, "plan"), Body=b"Plan: 2 to add\n"
    )
    url = auth_client.get(f"{API}/plans/{tfe_runs.plan_id(run_id)}").json()["data"]["attributes"]["log-read-url"]
    response = client.get(_log_path(url), params={"limit": 65536, "offset": 0})
    assert response.content == b"\x02Plan: 2 to add\n\x03"


def test_an_apply_log_carries_the_lines_the_cli_skips(auth_client, client, awaiting_confirmation):
    """The cloud backend drops an apply log's first three lines, so they are a preamble."""
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"{API}/runs/{run_id}/actions/discard", content=b"{}")
    url = auth_client.get(f"{API}/applies/{tfe_runs.apply_id(run_id)}").json()["data"]["attributes"]["log-read-url"]
    body = client.get(_log_path(url), params={"limit": 65536, "offset": 0}).content
    assert body.startswith(b"\x02" + tfe_runs.APPLY_LOG_PREAMBLE.encode())
    assert tfe_runs.APPLY_LOG_PREAMBLE.count("\n") == 3
    assert body.endswith(b"\x03")


def test_a_log_token_reads_only_its_own_phase(auth_client, client, created_run):
    """A plan's token does not read the apply, and a tampered one reads nothing."""
    run_id = created_run["run_id"]
    url = auth_client.get(f"{API}/plans/{tfe_runs.plan_id(run_id)}").json()["data"]["attributes"]["log-read-url"]
    token = url.rsplit("/", 1)[1]
    assert client.get(f"{API}/applies/{tfe_runs.apply_id(run_id)}/logs/{token}").status_code == 404
    assert client.get(f"{API}/plans/{tfe_runs.plan_id(run_id)}/logs/{token[:-2]}AA").status_code == 404


def test_an_expired_log_token_is_refused(settings):
    """A token reads until its expiry and not after."""
    issued = 1_000_000.0
    token = tfe_runs.log_token("plan-X", settings, now=issued)
    assert tfe_runs.verify_log_token("plan-X", token, settings, now=issued + 60)
    assert not tfe_runs.verify_log_token("plan-X", token, settings, now=issued + tfe_runs.LOG_URL_TTL_SECONDS + 1)
    assert not tfe_runs.verify_log_token("plan-Y", token, settings, now=issued + 60)


def test_the_queue_routes_answer_empty(auth_client, created_run):
    """The workspace's runs list its run; the organization queue and capacity are empty."""
    workspace_id = created_run["workspace_id"]
    runs = auth_client.get(f"{API}/workspaces/{workspace_id}/runs").json()
    assert [item["id"] for item in runs["data"]] == [created_run["run_id"]]
    assert runs["meta"]["pagination"]["total-count"] == 1
    queue = auth_client.get(f"{API}/organizations/WebbPulse/runs/queue").json()
    assert queue["data"] == []
    capacity = auth_client.get(f"{API}/organizations/WebbPulse/capacity").json()["data"]
    assert capacity["type"] == "organization-capacity"
    events = auth_client.get(f"{API}/runs/{created_run['run_id']}/run-events").json()
    assert events["data"] == []


def test_an_unknown_run_is_a_json_api_404(auth_client):
    """go-tfe maps a 404 to `ErrResourceNotFound`."""
    response = auth_client.get(f"{API}/runs/run-01JQZZZZZZZZZZZZZZZZZZZZZZ")
    assert response.status_code == 404
    assert response.json()["errors"][0]["status"] == "404"


def test_reading_a_run_needs_runs_read(scoped_client, created_run):
    """The run routes keep the scopes the v1 routes hold."""
    from app.common.core.auth import WORKSPACES_READ

    response = scoped_client(WORKSPACES_READ).get(f"{API}/runs/{created_run['run_id']}")
    assert response.status_code == 403


def test_settings_give_an_absolute_log_url(auth_client, created_run):
    """go-tfe parses the log URL as absolute, so it carries a scheme and host."""
    url = auth_client.get(f"{API}/plans/{tfe_runs.plan_id(created_run['run_id'])}").json()["data"]["attributes"][
        "log-read-url"
    ]
    parsed = urlparse(url)
    assert parsed.scheme in ("http", "https")
    assert parsed.netloc
    assert get_settings().API_BASE_URL in url
