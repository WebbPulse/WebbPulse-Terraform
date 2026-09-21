"""Creating, reading and listing runs, and the per workspace serialisation."""

import json

import boto3

from app.common.db.tables import RUNS, local_table_name
from app.domains.runs import service as runs_service
from app.domains.workspaces import service as workspaces_service
from tests.conftest import ENVIRONMENT, REGION, WORKSPACE_PAYLOAD

BASE = "/api/v1/runs"


def stored_run(run_id: str) -> dict:
    """The raw run row, read around the service so nothing is stripped."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(RUNS, ENVIRONMENT))
    return table.get_item(Key={"run_id": run_id}).get("Item", {})


def create_body(workspace_id: str, config_version_id: str, **overrides) -> dict:
    """A valid create body with `overrides` applied."""
    return {
        "workspace_id": workspace_id,
        "config_version_id": config_version_id,
        "plan_only": False,
        "message": "An example run.",
        **overrides,
    }


def test_create_starts_the_run(created_run):
    """A first run against a free workspace starts rather than queueing."""
    assert created_run["run_id"].startswith("run-")
    assert created_run["status"] == "planning"
    assert created_run["queued_behind"] is None
    assert created_run["started_at"]
    assert created_run["execution_arn"]


def test_create_returns_a_run_token(created_run):
    """A started run carries the token its runner will authenticate with."""
    assert created_run["run_token"].startswith("wpk_")


def test_the_run_token_is_stored_only_as_a_hash(created_run):
    """The plaintext token is not on the run row, only its hash."""
    item = stored_run(created_run["run_id"])
    assert item["run_token_hash"]
    assert created_run["run_token"] not in str(item)


def test_the_execution_input_carries_the_run_token(created_run):
    """The token travels on the execution input, which is how the runner gets it.

    The state machine reads `$.run_token` into both container overrides, so a
    start that omits it produces a task that exits before it fetches a bundle.
    """
    client = boto3.client("stepfunctions", region_name=REGION)
    described = client.describe_execution(executionArn=created_run["execution_arn"])
    execution_input = json.loads(described["input"])
    assert execution_input["run_token"] == created_run["run_token"]
    assert execution_input["run_id"] == created_run["run_id"]
    assert execution_input["workspace_id"]
    assert execution_input["plan_only"] is False


def test_starting_an_already_started_run_is_a_no_op(created_run):
    """A second start returns the run untouched rather than minting a second token.

    The execution is named for the run id, so a real second start would raise
    `ExecutionAlreadyExists`. Worse, it would overwrite the token hash the running
    execution's runner is already authenticating with, locking that runner out of
    its own bundle.
    """
    run_id = created_run["run_id"]
    first_hash = stored_run(run_id)["run_token_hash"]

    again = runs_service.start_run(run_id)

    assert again["execution_arn"] == created_run["execution_arn"]
    assert "run_token" not in again
    assert stored_run(run_id)["run_token_hash"] == first_hash


def test_a_failed_promotion_is_logged_and_leaves_the_transition_done(
    auth_client, workspace, uploaded_config_version, created_run, monkeypatch, caplog
):
    """A promotion that cannot start must not fail the transition that finished.

    Finishing a run has already been recorded by the time the next one is
    promoted, so raising here would leave the finished run non-terminal. The
    failure is logged instead, which is the only trace a stuck `pending` run has.
    """
    queued = auth_client.post(
        BASE,
        json=create_body(workspace["workspace_id"], uploaded_config_version["config_version_id"]),
    ).json()
    assert queued["status"] == "pending"

    def explode(*args, **kwargs):
        """Stand in for a Step Functions start that will not go through."""
        raise RuntimeError("step functions is unavailable")

    monkeypatch.setattr(runs_service, "start_run", explode)
    with caplog.at_level("ERROR"):
        finished = runs_service.finish_run(created_run["run_id"], "applied")

    assert finished["status"] == "applied"
    assert runs_service.get_run(queued["run_id"])["status"] == "pending"
    assert any(record.__dict__.get("event") == "runs.promote.failed" for record in caplog.records)


def test_create_is_404_for_an_absent_workspace(auth_client, uploaded_config_version):
    """A run cannot be created against a workspace that is not there."""
    response = auth_client.post(
        BASE,
        json=create_body("ws-01JBQ0000000000000000000AA", uploaded_config_version["config_version_id"]),
    )
    assert response.status_code == 404


def test_create_is_404_for_an_absent_config_version(auth_client, workspace):
    """A run cannot be created against a config version that is not there."""
    response = auth_client.post(
        BASE,
        json=create_body(workspace["workspace_id"], "cv-01JBQ0000000000000000000AA"),
    )
    assert response.status_code == 404


def test_create_is_409_for_a_config_version_never_uploaded(auth_client, workspace, state_machine):
    """A pending config version has no tarball, so a run on it is refused.

    Without this the runner would start, fetch a presigned GET for an object
    that does not exist and fail inside Terraform rather than at the API.
    """
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]
    response = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"]))
    assert response.status_code == 409, response.text


def test_create_records_plan_only_and_the_message(auth_client, workspace, uploaded_config_version, state_machine):
    """Both caller supplied fields land on the run."""
    response = auth_client.post(
        BASE,
        json=create_body(
            workspace["workspace_id"],
            uploaded_config_version["config_version_id"],
            plan_only=True,
            message="Just a plan.",
        ),
    )
    body = response.json()
    assert body["plan_only"] is True
    assert body["message"] == "Just a plan."


def test_a_second_run_queues_behind_the_first(
    auth_client, workspace, uploaded_config_version, state_machine, created_run
):
    """Runs are serial per workspace: the second waits rather than starting.

    Two concurrent executions against one workspace would contend on the S3
    state lock, so the second is queued here instead of failing there.
    """
    response = auth_client.post(
        BASE,
        json=create_body(workspace["workspace_id"], uploaded_config_version["config_version_id"]),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["queued_behind"] == created_run["run_id"]
    assert body["run_token"] is None
    assert body["execution_arn"] is None


def test_a_queued_run_starts_when_the_first_finishes(
    auth_client, workspace, uploaded_config_version, state_machine, created_run
):
    """Finishing the active run promotes the next queued one."""
    queued = auth_client.post(
        BASE,
        json=create_body(workspace["workspace_id"], uploaded_config_version["config_version_id"]),
    ).json()

    runs_service.finish_run(created_run["run_id"], "applied")

    promoted = auth_client.get(f"{BASE}/{queued['run_id']}").json()
    assert promoted["status"] == "planning"
    assert promoted["queued_behind"] is None
    assert promoted["started_at"]


def test_only_one_queued_run_is_promoted(auth_client, workspace, uploaded_config_version, state_machine, created_run):
    """Promotion starts one run, so the queue stays serial rather than draining."""
    workspace_id = workspace["workspace_id"]
    config_version_id = uploaded_config_version["config_version_id"]
    first = auth_client.post(BASE, json=create_body(workspace_id, config_version_id)).json()
    second = auth_client.post(BASE, json=create_body(workspace_id, config_version_id)).json()

    runs_service.finish_run(created_run["run_id"], "applied")

    assert auth_client.get(f"{BASE}/{first['run_id']}").json()["status"] == "planning"
    assert auth_client.get(f"{BASE}/{second['run_id']}").json()["status"] == "pending"


def test_a_run_on_another_workspace_is_not_queued(
    auth_client, workspace, uploaded_config_version, state_machine, created_run
):
    """Serialisation is per workspace, not global to the environment."""
    from app.domains.workspaces import service as workspaces_service

    other = auth_client.post("/api/v1/workspaces", json={**WORKSPACE_PAYLOAD, "name": "other"}).json()
    other_id = other["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{other_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]
    workspaces_service.mark_config_version_uploaded(version["config_version_id"])

    response = auth_client.post(BASE, json=create_body(other_id, version["config_version_id"]))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "planning"


def test_get_returns_the_run(auth_client, created_run):
    """A run reads back by id."""
    response = auth_client.get(f"{BASE}/{created_run['run_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["run_id"] == created_run["run_id"]


def test_get_never_returns_the_task_token(auth_client, awaiting_confirmation):
    """The confirm task token is stored but never served.

    A caller holding it could confirm the run through Step Functions directly,
    with no scope check at all.
    """
    run_id = awaiting_confirmation["run_id"]
    assert stored_run(run_id)["confirm_task_token"]
    body = auth_client.get(f"{BASE}/{run_id}").json()
    assert "confirm_task_token" not in body
    assert "run_token_hash" not in body


def test_get_is_404_for_an_absent_run(auth_client):
    """A well formed id naming nothing is a 404."""
    assert auth_client.get(f"{BASE}/run-01JBQ0000000000000000000AA").status_code == 404


def test_get_is_422_for_a_malformed_id(auth_client):
    """An id that is not a `run-` ULID never reaches the table."""
    assert auth_client.get(f"{BASE}/nonsense").status_code == 422


def test_list_requires_a_workspace(auth_client):
    """The list is per workspace, so the query parameter is required."""
    assert auth_client.get(BASE).status_code == 422


def test_list_returns_the_workspaces_runs(auth_client, workspace, created_run):
    """The list carries the workspace's runs."""
    response = auth_client.get(BASE, params={"workspace_id": workspace["workspace_id"]})
    assert response.status_code == 200, response.text
    assert [item["run_id"] for item in response.json()["items"]] == [created_run["run_id"]]


def test_list_is_newest_first(auth_client, workspace, uploaded_config_version, state_machine, created_run):
    """Runs come back newest first, which is what a history view wants."""
    workspace_id = workspace["workspace_id"]
    second = auth_client.post(
        BASE,
        json=create_body(workspace_id, uploaded_config_version["config_version_id"]),
    ).json()
    items = auth_client.get(BASE, params={"workspace_id": workspace_id}).json()["items"]
    assert [item["run_id"] for item in items] == [second["run_id"], created_run["run_id"]]


def test_list_is_404_for_an_absent_workspace(auth_client):
    """Listing against nothing is a 404 rather than an empty list."""
    response = auth_client.get(BASE, params={"workspace_id": "ws-01JBQ0000000000000000000AA"})
    assert response.status_code == 404


def test_list_is_empty_for_a_workspace_with_no_runs(auth_client, workspace):
    """A workspace that never ran lists nothing."""
    response = auth_client.get(BASE, params={"workspace_id": workspace["workspace_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_create_is_409_when_the_workspace_has_no_run_role(auth_client, uploaded_config_version, state_machine):
    """A run against a workspace with nothing to assume is refused before it starts."""
    workspace_id = uploaded_config_version["workspace_id"]
    workspaces_service.update_workspace(workspace_id, {"run_role_arn": ""})

    response = auth_client.post(
        BASE,
        json=create_body(workspace_id, uploaded_config_version["config_version_id"]),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RUN_ROLE_MISSING"


def test_the_missing_run_role_conflict_leaves_no_run_behind(auth_client, uploaded_config_version, state_machine):
    """The workspace is checked before anything is written, so nothing is."""
    workspace_id = uploaded_config_version["workspace_id"]
    workspaces_service.update_workspace(workspace_id, {"run_role_arn": ""})

    auth_client.post(
        BASE,
        json=create_body(workspace_id, uploaded_config_version["config_version_id"]),
    )
    listed = auth_client.get(BASE, params={"workspace_id": workspace_id})
    assert listed.json()["items"] == []


def test_a_run_starts_against_a_version_whose_object_landed(auth_client, workspace, state_machine):
    """Uploading to the presigned PUT is enough to start a run.

    The first staging e2e run failed exactly here: the tarball was in the bucket
    and creating the run still answered 409, because nothing reconciled the row.
    """
    import boto3

    from tests.conftest import ARTIFACTS_BUCKET, REGION

    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=ARTIFACTS_BUCKET, Key=version["key"], Body=b"tarball", ContentType="application/gzip"
    )

    response = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"]))
    assert response.status_code == 201, response.text


def test_a_run_is_409_while_the_tarball_never_landed(auth_client, workspace, state_machine):
    """A config version with no object in the bucket still refuses a run."""
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]

    response = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"]))
    assert response.status_code == 409, response.text


def stored_config_version_status(config_version_id: str) -> str:
    """The status on the raw config version row, read around the service."""
    from app.common.db.tables import CONFIG_VERSIONS

    table = boto3.resource("dynamodb", region_name=REGION).Table(local_table_name(CONFIG_VERSIONS, ENVIRONMENT))
    item = table.get_item(Key={"config_version_id": config_version_id}).get("Item", {})
    return str(item.get("status", ""))


def put_config_object(key: str) -> None:
    """Land a tarball at `key`, the way a client's presigned PUT would."""
    from tests.conftest import ARTIFACTS_BUCKET

    boto3.client("s3", region_name=REGION).put_object(
        Bucket=ARTIFACTS_BUCKET, Key=key, Body=b"tarball", ContentType="application/gzip"
    )


def test_create_accepts_a_pending_version_whose_object_landed(auth_client, workspace, state_machine):
    """An upload S3 never announced still starts a run, from the HEAD alone."""
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]
    put_config_object(version["key"])

    response = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"]))

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "planning"


def test_create_does_not_write_the_config_version_row(auth_client, workspace, state_machine):
    """Run creation never calls UpdateItem on config-versions.

    The runs function role reads these rows under a read only grant, so a write
    from here is an AccessDeniedException and a 500. The row staying `pending`
    is what proves the reconciliation did not persist.
    """
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]
    put_config_object(version["key"])

    response = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"]))

    assert response.status_code == 201, response.text
    assert stored_config_version_status(version["config_version_id"]) == "pending"


def test_the_bundle_does_not_write_the_config_version_row(app, auth_client, workspace, state_machine):
    """Fetching the bundle never calls UpdateItem on config-versions either.

    The bundle is read under the same read only grant as run creation, against a
    row the workspaces domain may not have flipped yet. The row staying `pending`
    while the bundle answers 200 is what proves the reconciliation did not persist.
    """
    from fastapi.testclient import TestClient

    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]
    put_config_object(version["key"])
    created = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"])).json()

    with TestClient(app, headers={"Authorization": f"Bearer {created['run_token']}"}) as runner:
        response = runner.get(f"{BASE}/{created['run_id']}/bundle")

    assert response.status_code == 200, response.text
    assert stored_config_version_status(version["config_version_id"]) == "pending"


def test_create_is_409_when_the_pending_versions_object_is_absent(auth_client, workspace, state_machine):
    """No tarball in the bucket is still `ConfigVersionNotReady`."""
    workspace_id = workspace["workspace_id"]
    version = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    ).json()["config_version"]

    response = auth_client.post(BASE, json=create_body(workspace_id, version["config_version_id"]))

    assert response.status_code == 409, response.text
    assert stored_config_version_status(version["config_version_id"]) == "pending"
