"""The ingest consumer: an uploaded tarball becoming runs on the bound workspaces.

Each upload goes through the real route, with the fake GitHub verifier, and the
tarball is PUT straight into moto's bucket, so the record the consumer reads is the
one the route wrote. The consumer is driven both directly and through the events
route the Lambda Web Adapter posts to.
"""

import json

import boto3
import pytest
from fastapi.testclient import TestClient
from vcs_helpers import BASE_SHA, HEAD_SHA, REPO, FakeVerifier, claims, pr_claims, tarball, token_for
from webbpulse.events import BATCH_FAILURES_KEY, events_path

from app.common.composition.wiring import build_domain_app
from app.common.db import repositories
from app.domains.runs import service as runs_service
from app.domains.runs import vcs
from app.domains.runs.consumers import ingest
from app.domains.workspaces import service as workspaces_service

ROLE = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run"


@pytest.fixture(autouse=True)
def verifier(monkeypatch):
    """The fake GitHub verifier the route resolves."""
    fake = FakeVerifier()
    monkeypatch.setattr(vcs, "_verifier", lambda audience: fake)
    return fake


@pytest.fixture
def bind(auth_client):
    """A factory creating a workspace bound to the test repository."""

    def create(name: str = "infra", **fields):
        payload = {
            "name": name,
            "engine_version": "1.11.4",
            "run_role_arn": ROLE,
            "vcs_repo": REPO,
            "tracked_branch": "main",
        } | fields
        response = auth_client.post("/api/v1/workspaces", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return create


def upload(client, settings, values, data: bytes, **body) -> dict:
    """Request an upload for `values`, PUT `data` to its key, and return the event body."""
    payload = {"sha": HEAD_SHA, "pr_number": None, "base_sha": None, "size_bytes": len(data)} | body
    response = client.post(
        "/api/v1/vcs/uploads",
        json=payload,
        headers={"Authorization": f"Bearer {token_for(values)}"},
    )
    assert response.status_code == 201, response.text
    key = vcs.ingest_key(response.json()["upload_id"])
    boto3.client("s3", region_name="us-west-2").put_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key, Body=data)
    return {"kind": ingest.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": key, "size": len(data)}


def deliver(event: dict, settings) -> list[str]:
    """Hand one message to the consumer."""
    return ingest.handle_record({"body": json.dumps(event)}, settings=settings)


def runs_on(workspace: dict, settings) -> list[dict]:
    """Every run on one workspace, oldest first."""
    return sorted(runs_service.list_runs(workspace["workspace_id"], settings=settings), key=lambda run: run["run_id"])


def test_a_push_to_the_tracked_branch_starts_a_normal_run(client, settings, bind, state_machine):
    """The run is not plan only, is sourced `vcs_push`, and carries the commit."""
    workspace = bind()
    [run_id] = deliver(upload(client, settings, claims(), tarball()), settings)
    run = runs_service.get_run(run_id, settings=settings)
    assert run["workspace_id"] == workspace["workspace_id"]
    assert run["plan_only"] is False
    assert run["source"] == "vcs_push"
    assert run["status"] == "planning"
    assert run["vcs"]["repo"] == REPO
    assert run["vcs"]["sha"] == HEAD_SHA
    assert run["vcs"]["branch"] == "main"
    assert run["actor"] == {"kind": "vcs", "id": "github:octocat", "display_name": "octocat"}


def test_the_config_version_is_copied_and_uploaded(client, settings, bind, state_machine):
    """The tarball lands at the workspace's config key and the row reads uploaded."""
    workspace = bind()
    data = tarball()
    [run_id] = deliver(upload(client, settings, claims(), data), settings)
    run = runs_service.get_run(run_id, settings=settings)
    row = repositories.config_versions(settings).get({"config_version_id": run["config_version_id"]}) or {}
    assert row["workspace_id"] == workspace["workspace_id"]
    assert row["status"] == "uploaded"
    copied = boto3.client("s3", region_name="us-west-2").get_object(Bucket=settings.ARTIFACTS_BUCKET, Key=row["key"])
    assert copied["Body"].read() == data


def test_a_pull_request_starts_a_plan_only_run(client, settings, bind, state_machine):
    """Sourced `vcs_pr`, with the number from the token and the unverified shas kept."""
    bind()
    event = upload(client, settings, pr_claims(12), tarball(), base_sha=BASE_SHA)
    [run_id] = deliver(event, settings)
    run = runs_service.get_run(run_id, settings=settings)
    assert run["plan_only"] is True
    assert run["source"] == "vcs_pr"
    assert int(run["vcs"]["pr_number"]) == 12
    assert run["vcs"]["head_sha"] == HEAD_SHA
    assert run["vcs"]["base_sha"] == BASE_SHA
    assert "branch" not in run["vcs"]


def test_a_push_to_another_branch_starts_nothing(client, settings, bind, state_machine):
    """The branch filter reads the branch from the token's ref."""
    workspace = bind()
    values = claims(ref="refs/heads/feature")
    assert deliver(upload(client, settings, values, tarball()), settings) == []
    assert runs_on(workspace, settings) == []


def test_a_workspace_with_no_tracked_branch_ignores_pushes(client, settings, bind, state_machine):
    """No branch, no push runs."""
    bind(tracked_branch=None)
    assert deliver(upload(client, settings, claims(), tarball()), settings) == []


def test_speculative_plans_off_ignores_pull_requests(client, settings, bind, state_machine):
    """A workspace can opt out of pull request plans."""
    bind(speculative_plans=False)
    assert deliver(upload(client, settings, pr_claims(3), tarball()), settings) == []


def test_the_default_pattern_is_the_working_directory(client, settings, bind, state_machine):
    """Empty patterns mean everything under `working_directory`."""
    inside = bind("inside", working_directory="stacks/app")
    outside = bind("outside", working_directory="stacks/other")
    deliver(upload(client, settings, claims(), tarball("stacks/app/main.tf\n")), settings)
    assert len(runs_on(inside, settings)) == 1
    assert runs_on(outside, settings) == []


def test_trigger_patterns_glob_recursively(client, settings, bind, state_machine):
    """`**` crosses directories and `*` does not."""
    deep = bind("deep", trigger_patterns=["modules/**"])
    shallow = bind("shallow", trigger_patterns=["modules/*"])
    deliver(upload(client, settings, claims(), tarball("modules/net/vpc.tf\n")), settings)
    assert len(runs_on(deep, settings)) == 1
    assert runs_on(shallow, settings) == []


def test_a_star_changed_path_matches_every_workspace(client, settings, bind, state_machine):
    """The workflow writes `*` when it could not diff."""
    workspace = bind(trigger_patterns=["nowhere/**"])
    deliver(upload(client, settings, claims(), tarball("*\n")), settings)
    assert len(runs_on(workspace, settings)) == 1


def test_a_missing_changed_paths_file_matches_every_workspace(client, settings, bind, state_machine):
    """No list means nothing can be ruled out."""
    workspace = bind(trigger_patterns=["nowhere/**"])
    deliver(upload(client, settings, claims(), tarball(None)), settings)
    assert len(runs_on(workspace, settings)) == 1


def test_a_workspace_with_no_run_role_is_skipped(client, settings, bind, state_machine):
    """It is skipped rather than failing the other workspaces."""
    ready = bind("ready")
    roleless = bind("roleless", run_role_arn=None)
    deliver(upload(client, settings, claims(), tarball()), settings)
    assert len(runs_on(ready, settings)) == 1
    assert runs_on(roleless, settings) == []


def test_a_redelivery_creates_nothing_new(client, settings, bind, state_machine):
    """Delivery is at least once: the same message twice is one run."""
    workspace = bind()
    event = upload(client, settings, claims(), tarball())
    first = deliver(event, settings)
    second = deliver(event, settings)
    assert first == second
    assert len(runs_on(workspace, settings)) == 1
    assert len(runs_service.list_runs(workspace["workspace_id"], settings=settings)) == 1


def test_a_retried_put_firing_twice_is_one_run(client, settings, bind, state_machine):
    """curl retries the PUT, so S3 can emit two events for one key. One run results."""
    workspace = bind()
    data = tarball()
    event = upload(client, settings, claims(), data)
    boto3.client("s3", region_name="us-west-2").put_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=event["key"], Body=data
    )
    deliver(event, settings)
    deliver(dict(event), settings)
    runs = runs_on(workspace, settings)
    assert len(runs) == 1
    assert len(workspaces_service.list_config_versions(workspace["workspace_id"], settings=settings)) == 1


def test_a_retried_request_and_a_retried_put_are_still_one_run(client, settings, bind, state_machine):
    """The route's retry returns the same key, so the whole retried path is one run."""
    workspace = bind()
    data = tarball()
    first = upload(client, settings, claims(), data)
    second = upload(client, settings, claims(), data)
    assert first == second
    deliver(first, settings)
    deliver(second, settings)
    assert len(runs_on(workspace, settings)) == 1


def test_a_run_stored_but_never_started_is_started_on_redelivery(client, settings, bind, state_machine, monkeypatch):
    """A crash between the put and the start is healed by the retry."""
    workspace = bind()
    event = upload(client, settings, claims(), tarball())
    original = runs_service.start_run

    def crash(run_id, *, settings=None):
        raise RuntimeError("lost the Lambda")

    monkeypatch.setattr(runs_service, "start_run", crash)
    with pytest.raises(RuntimeError):
        deliver(event, settings)
    [stored] = runs_on(workspace, settings)
    assert stored["status"] == "pending"
    assert not stored.get("execution_arn")

    monkeypatch.setattr(runs_service, "start_run", original)
    deliver(event, settings)
    [started] = runs_on(workspace, settings)
    assert started["run_id"] == stored["run_id"]
    assert started["status"] == "planning"


def test_the_consumer_trusts_the_record_not_the_object(client, settings, bind, state_machine):
    """An object under `ingest/` with no record starts nothing."""
    bind()
    key = "ingest/up-0000000000000000000000000Z.tar.gz"
    data = tarball()
    boto3.client("s3", region_name="us-west-2").put_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=key, Body=data, Metadata={"repo": REPO, "event": "push"}
    )
    event = {"kind": ingest.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": key, "size": len(data)}
    assert deliver(event, settings) == []


@pytest.mark.parametrize("key", ["ingest/whatever.tar.gz", "configs/ws-x/cv-y.tar.gz", "ingest/up-short.tar.gz"])
def test_a_key_outside_the_upload_shape_is_dropped(settings, key):
    """Only `ingest/<upload id>.tar.gz` names an upload."""
    event = {"kind": ingest.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": key, "size": 1}
    assert deliver(event, settings) == []


def test_a_foreign_bucket_is_dropped(client, settings, bind, state_machine):
    """The rule filters on the artifacts bucket; anything else is not ours."""
    bind()
    event = upload(client, settings, claims(), tarball()) | {"bucket": "someone-else"}
    assert deliver(event, settings) == []


def test_a_size_the_record_does_not_declare_is_dropped(client, settings, bind, state_machine):
    """The object has to be the one the record signed for."""
    workspace = bind()
    event = upload(client, settings, claims(), tarball()) | {"size": 1}
    assert deliver(event, settings) == []
    assert runs_on(workspace, settings) == []


def test_a_newer_push_cancels_an_older_pending_one(client, settings, bind, state_machine):
    """Supersede: an older queued push run on the same branch is cancelled."""
    workspace = bind()
    first = deliver(upload(client, settings, claims(run_id="1"), tarball()), settings)[0]
    second = deliver(upload(client, settings, claims(run_id="2", sha="d" * 40), tarball()), settings)[0]
    third = deliver(upload(client, settings, claims(run_id="3", sha="e" * 40), tarball()), settings)[0]
    assert runs_service.get_run(first, settings=settings)["status"] == "planning"
    assert runs_service.get_run(second, settings=settings)["status"] == "cancelled"
    assert runs_service.get_run(third, settings=settings)["status"] == "pending"
    assert len(runs_on(workspace, settings)) == 3


def test_a_newer_push_discards_an_older_run_awaiting_confirmation(client, settings, bind, state_machine):
    """Supersede: an older plan waiting on a person is discarded."""
    bind()
    first = deliver(upload(client, settings, claims(run_id="1"), tarball()), settings)[0]
    repositories.runs(settings).update(
        {"run_id": first},
        update_expression="SET #s = :s",
        expression_names={"#s": "status"},
        expression_values={":s": "awaiting_confirmation"},
    )
    deliver(upload(client, settings, claims(run_id="2", sha="d" * 40), tarball()), settings)
    assert runs_service.get_run(first, settings=settings)["status"] == "discarded"


def test_a_pull_request_supersedes_only_its_own_number(client, settings, bind, state_machine):
    """Another pull request's queued plan is left alone."""
    bind()
    deliver(upload(client, settings, claims(run_id="1"), tarball()), settings)
    other = deliver(upload(client, settings, pr_claims(5, run_id="2"), tarball()), settings)[0]
    older = deliver(upload(client, settings, pr_claims(6, run_id="3"), tarball()), settings)[0]
    newer = deliver(upload(client, settings, pr_claims(6, run_id="4", sha="f" * 40), tarball()), settings)[0]
    assert runs_service.get_run(other, settings=settings)["status"] == "pending"
    assert runs_service.get_run(older, settings=settings)["status"] == "cancelled"
    assert runs_service.get_run(newer, settings=settings)["status"] == "pending"


def test_a_push_does_not_supersede_an_api_run(client, settings, bind, auth_client, state_machine):
    """Only runs from the same source are replaced."""
    workspace = bind()
    deliver(upload(client, settings, claims(run_id="1"), tarball()), settings)
    [running] = runs_on(workspace, settings)
    created = auth_client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/config-versions", json={"size_bytes": 10}
    )
    config_version_id = created.json()["config_version"]["config_version_id"]
    workspaces_service.mark_config_version_uploaded(config_version_id)
    api_run = auth_client.post(
        "/api/v1/runs",
        json={"workspace_id": workspace["workspace_id"], "config_version_id": config_version_id},
    ).json()
    assert api_run["status"] == "pending"
    deliver(upload(client, settings, claims(run_id="2", sha="d" * 40), tarball()), settings)
    assert runs_service.get_run(api_run["run_id"], settings=settings)["status"] == "pending"
    assert runs_service.get_run(running["run_id"], settings=settings)["status"] == "planning"


def test_a_late_older_upload_is_not_run_after_a_newer_one(client, settings, bind, state_machine):
    """Delivered out of order, the older upload starts nothing."""
    workspace = bind()
    older = upload(client, settings, claims(run_id="1"), tarball())
    newer = upload(client, settings, claims(run_id="2", sha="d" * 40), tarball())
    deliver(newer, settings)
    assert deliver(older, settings) == []
    assert len(runs_on(workspace, settings)) == 1


def test_one_upload_runs_on_every_bound_workspace(client, settings, bind, state_machine):
    """Two workspaces on one repository each get their own run."""
    first = bind("first")
    second = bind("second")
    run_ids = deliver(upload(client, settings, claims(), tarball()), settings)
    assert len(run_ids) == 2
    assert len(runs_on(first, settings)) == 1
    assert len(runs_on(second, settings)) == 1


def test_the_events_route_dispatches_an_ingest(client, settings, bind, state_machine):
    """The adapter's pass-through path routes `config_ingested` to this consumer."""
    workspace = bind()
    event = upload(client, settings, claims(), tarball())
    runs_app = TestClient(build_domain_app("runs", settings=settings))
    response = runs_app.post(
        events_path(),
        json={"Records": [{"messageId": "m1", "body": json.dumps(event), "eventSource": "aws:sqs"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()[BATCH_FAILURES_KEY] == []
    assert len(runs_on(workspace, settings)) == 1


def test_a_malformed_body_fails_the_record(settings):
    """A body that is not an object created event parks on the dead letter queue."""
    with pytest.raises(ingest.MalformedIngest):
        ingest.handle_record({"body": json.dumps({"kind": ingest.INGEST_KIND})}, settings=settings)


@pytest.mark.parametrize(
    ("patterns", "paths", "expected"),
    [
        (["**"], ["a/b/c.tf"], True),
        (["*.tf"], ["main.tf"], True),
        (["*.tf"], ["dir/main.tf"], False),
        (["dir/**"], ["dir/a/b.tf"], True),
        (["dir/**"], ["other/b.tf"], False),
        (["dir/*.tf"], ["dir/main.tf"], True),
        (["**/*.tf"], ["deep/er/x.tf"], True),
        ([".github/**"], [".github/workflows/x.yml"], True),
        (["dir/**"], None, True),
        (["dir/**"], [], False),
    ],
)
def test_paths_match(patterns, paths, expected):
    """Patterns are globs over repository paths, `**` recursive, hidden files included."""
    assert ingest.paths_match(patterns, paths) is expected


def test_the_default_trigger_pattern():
    """The working directory, trimmed, or everything."""
    assert ingest.trigger_patterns({"working_directory": "./stacks/app/"}) == ["stacks/app/**"]
    assert ingest.trigger_patterns({"working_directory": ""}) == ["**"]
    assert ingest.trigger_patterns({"trigger_patterns": ["x/*"], "working_directory": "y"}) == ["x/*"]
