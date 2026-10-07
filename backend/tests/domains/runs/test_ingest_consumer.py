"""The ingest consumer: an uploaded tarball becoming runs on the bound workspaces.

Each upload writes an ingest record shaped like the webhook consumer's and puts the
tarball straight into moto's bucket, so the consumer reads a record and an object
exactly as a GitHub App delivery leaves them. The consumer is driven both directly and through the events
route the Lambda Web Adapter posts to.
"""

import hashlib
import json
from datetime import datetime, timezone

import boto3
import pytest
from fastapi.testclient import TestClient
from vcs_helpers import BASE_SHA, HEAD_SHA, REPO, claims, pr_claims, tarball
from webbpulse.events import BATCH_FAILURES_KEY, events_path

from app.common.composition.wiring import build_domain_app
from app.common.db import repositories
from app.common.db.tables import CONFIG_VERSIONS, local_table_name, table_definition
from app.domains.runs import reporting, vcs
from app.domains.runs import service as runs_service
from app.domains.runs.consumers import ingest
from app.domains.workspaces import service as workspaces_service

ROLE = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run"


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


@pytest.fixture(autouse=True)
def reported(monkeypatch):
    """The uploads the consumer reported itself, as upload id and skipped workspaces."""
    calls: list[tuple[str, list[dict]]] = []

    def record(upload, skipped, *, settings=None):
        calls.append((str(upload["upload_id"]), [dict(item) for item in skipped]))
        return True

    monkeypatch.setattr(reporting, "report_upload", record)
    return calls


def record_for(values: dict, data: bytes, *, head_sha: str | None, base_sha: str | None) -> dict:
    """The ingest record one delivery described by `values` writes, keyed by a stable upload id."""
    seed = ":".join(str(values[name]) for name in ("repository_id", "sha", "run_id", "run_attempt"))
    upload_id = vcs.UPLOAD_ID_PREFIX + vcs.crockford(hashlib.sha256(seed.encode()).digest())
    now = datetime.now(timezone.utc)
    item = {
        "upload_id": upload_id,
        "key": vcs.ingest_key(upload_id),
        "repo": str(values["repository"]),
        "repository_id": str(values["repository_id"]),
        "repository_owner": str(values["repository_owner"]),
        "event": str(values["event_name"]),
        "ref": str(values["ref"]),
        "sha": str(values["sha"]),
        "actor": str(values["actor"]),
        "size_bytes": len(data),
        "created_at": now.isoformat(),
        "created_at_ms": int(now.timestamp() * 1000),
        "expires_at": int((now + vcs.RECORD_TTL).timestamp()),
    }
    ref = str(values["ref"])
    if values["event_name"] == vcs.PUSH_EVENT:
        item["branch"] = ref.removeprefix("refs/heads/")
    else:
        item["pr_number"] = int(ref.split("/")[2])
        if head_sha:
            item["head_sha"] = head_sha
        if base_sha:
            item["base_sha"] = base_sha
    return item


def upload(settings, values, data: bytes, *, sha: str = HEAD_SHA, base_sha: str | None = None) -> dict:
    """Write the ingest record for `values`, put `data` at its key, and return the event body."""
    item = record_for(values, data, head_sha=sha, base_sha=base_sha)
    repositories.vcs_uploads(settings).put(item)
    key = item["key"]
    boto3.client("s3", region_name="us-west-2").put_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key, Body=data)
    return {"kind": ingest.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": key, "size": len(data)}


def deliver(event: dict, settings) -> list[str]:
    """Hand one message to the consumer."""
    return ingest.handle_record({"body": json.dumps(event)}, settings=settings)


def runs_on(workspace: dict, settings) -> list[dict]:
    """Every run on one workspace, oldest first."""
    return sorted(runs_service.list_runs(workspace["workspace_id"], settings=settings), key=lambda run: run["run_id"])


def test_a_push_to_the_tracked_branch_starts_a_normal_run(settings, bind, state_machine):
    """The run is not plan only, is sourced `vcs_push`, and carries the commit."""
    workspace = bind()
    [run_id] = deliver(upload(settings, claims(), tarball()), settings)
    run = runs_service.get_run(run_id, settings=settings)
    assert run["workspace_id"] == workspace["workspace_id"]
    assert run["plan_only"] is False
    assert run["source"] == "vcs_push"
    assert run["status"] == "planning"
    assert run["vcs"]["repo"] == REPO
    assert run["vcs"]["sha"] == HEAD_SHA
    assert run["vcs"]["branch"] == "main"
    assert run["actor"] == {"kind": "vcs", "id": "github:octocat", "display_name": "octocat"}


def test_the_config_version_is_copied_and_uploaded(settings, bind, state_machine):
    """The tarball lands at the workspace's config key and the row reads uploaded."""
    workspace = bind()
    data = tarball()
    [run_id] = deliver(upload(settings, claims(), data), settings)
    run = runs_service.get_run(run_id, settings=settings)
    row = repositories.config_versions(settings).get({"config_version_id": run["config_version_id"]}) or {}
    assert row["workspace_id"] == workspace["workspace_id"]
    assert row["status"] == "uploaded"
    copied = boto3.client("s3", region_name="us-west-2").get_object(Bucket=settings.ARTIFACTS_BUCKET, Key=row["key"])
    assert copied["Body"].read() == data


def test_the_config_version_is_read_by_its_table_key_alone(settings, bind, state_machine, primary_keys_only):
    """The existence check names `config_version_id` only, the one key the table has.

    The table's `by_workspace` index is keyed on `workspace_id`, which is what let
    moto accept the extra key; the real schema in `terraform/dynamodb.tf` does not.
    """
    physical = local_table_name(CONFIG_VERSIONS, "test")
    definition = table_definition(CONFIG_VERSIONS, physical)
    assert [part["AttributeName"] for part in definition["KeySchema"]] == ["config_version_id"]
    bind()
    [run_id] = deliver(upload(settings, claims(), tarball()), settings)
    assert runs_service.get_run(run_id, settings=settings)["status"] == "planning"
    config_reads = {key.names for key in primary_keys_only if key.table == settings.CONFIG_VERSIONS_TABLE}
    assert config_reads == {frozenset({"config_version_id"})}


def test_a_pull_request_starts_a_plan_only_run(settings, bind, state_machine):
    """Sourced `vcs_pr`, with the number from the token and the unverified shas kept."""
    bind()
    event = upload(settings, pr_claims(12), tarball(), base_sha=BASE_SHA)
    [run_id] = deliver(event, settings)
    run = runs_service.get_run(run_id, settings=settings)
    assert run["plan_only"] is True
    assert run["source"] == "vcs_pr"
    assert int(run["vcs"]["pr_number"]) == 12
    assert run["vcs"]["head_sha"] == HEAD_SHA
    assert run["vcs"]["base_sha"] == BASE_SHA
    assert "branch" not in run["vcs"]


def test_a_push_to_another_branch_starts_nothing(settings, bind, state_machine):
    """The branch filter reads the branch from the token's ref."""
    workspace = bind()
    values = claims(ref="refs/heads/feature")
    assert deliver(upload(settings, values, tarball()), settings) == []
    assert runs_on(workspace, settings) == []


def test_a_workspace_with_no_tracked_branch_ignores_pushes(settings, bind, state_machine):
    """No branch, no push runs."""
    bind(tracked_branch=None)
    assert deliver(upload(settings, claims(), tarball()), settings) == []


def test_speculative_plans_off_ignores_pull_requests(settings, bind, state_machine):
    """A workspace can opt out of pull request plans."""
    bind(speculative_plans=False)
    assert deliver(upload(settings, pr_claims(3), tarball()), settings) == []


def upload_into(settings, base: str, data: bytes, *, default: str | None = "main") -> dict:
    """Write a pull request into `base` from the webhook bridge and return the event body."""
    item = record_for(pr_claims(5), data, head_sha=HEAD_SHA, base_sha=BASE_SHA)
    item["base_branch"] = base
    if default:
        item["default_branch"] = default
    repositories.vcs_uploads(settings).put(item)
    boto3.client("s3", region_name="us-west-2").put_object(Bucket=settings.ARTIFACTS_BUCKET, Key=item["key"], Body=data)
    return {"kind": ingest.INGEST_KIND, "bucket": settings.ARTIFACTS_BUCKET, "key": item["key"], "size": len(data)}


def test_a_pull_request_plans_only_the_workspaces_tracking_its_base(settings, bind, state_machine):
    """A pull request into `staging` plans the staging workspace and leaves the one tracking `main` alone."""
    prod = bind("prod", tracked_branch="main")
    staging = bind("prod-staging", tracked_branch="staging")
    [run_id] = deliver(upload_into(settings, "staging", tarball()), settings)
    assert runs_service.get_run(run_id, settings=settings)["workspace_id"] == staging["workspace_id"]
    assert runs_on(prod, settings) == []


def test_a_promotion_into_main_plans_only_the_main_workspaces(settings, bind, state_machine):
    """A pull request into `main` plans the workspace tracking `main` and not the staging one."""
    prod = bind("prod", tracked_branch="main")
    staging = bind("prod-staging", tracked_branch="staging")
    [run_id] = deliver(upload_into(settings, "main", tarball()), settings)
    assert runs_service.get_run(run_id, settings=settings)["workspace_id"] == prod["workspace_id"]
    assert runs_on(staging, settings) == []


@pytest.mark.parametrize(
    ("tracked", "base", "default", "plans"),
    [
        ("main", "main", "main", True),
        ("main", "staging", "main", False),
        (None, "main", "main", True),
        (None, "staging", "main", False),
        (None, "staging", None, True),
        ("main", None, "main", True),
    ],
    ids=["tracked", "other-base", "default", "default-other-base", "unknown-default", "no-base"],
)
def test_a_workspace_with_no_tracked_branch_targets_the_default_branch(tracked, base, default, plans):
    """The tracked branch, else the default branch, must be the base; an unknown side keeps planning."""
    upload_record = {"event": "pull_request", "base_branch": base, "default_branch": default}
    assert ingest.targets_base({"tracked_branch": tracked}, upload_record) is plans


def test_the_default_pattern_is_the_working_directory(settings, bind, state_machine):
    """Empty patterns mean everything under `working_directory`."""
    inside = bind("inside", working_directory="stacks/app")
    outside = bind("outside", working_directory="stacks/other")
    deliver(upload(settings, claims(), tarball("stacks/app/main.tf\n")), settings)
    assert len(runs_on(inside, settings)) == 1
    assert runs_on(outside, settings) == []


def test_trigger_patterns_glob_recursively(settings, bind, state_machine):
    """`**` crosses directories and `*` does not."""
    deep = bind("deep", trigger_patterns=["modules/**"])
    shallow = bind("shallow", trigger_patterns=["modules/*"])
    deliver(upload(settings, claims(), tarball("modules/net/vpc.tf\n")), settings)
    assert len(runs_on(deep, settings)) == 1
    assert runs_on(shallow, settings) == []


def test_always_trigger_runs_ignores_the_changed_paths(settings, bind, state_machine):
    """`file_triggers_enabled` false runs on any change, the working directory included or not."""
    always = bind("always", working_directory="stacks/app", file_triggers_enabled=False)
    filtered = bind("filtered", working_directory="stacks/app")
    deliver(upload(settings, claims(), tarball("docs/readme.md\n")), settings)
    assert len(runs_on(always, settings)) == 1
    assert runs_on(filtered, settings) == []


def test_always_triggers_reads_the_flag():
    """Absent means filtered, the default."""
    assert ingest.always_triggers({"file_triggers_enabled": False}) is True
    assert ingest.always_triggers({"file_triggers_enabled": True}) is False
    assert ingest.always_triggers({}) is False


def test_a_star_changed_path_matches_every_workspace(settings, bind, state_machine):
    """The workflow writes `*` when it could not diff."""
    workspace = bind(trigger_patterns=["nowhere/**"])
    deliver(upload(settings, claims(), tarball("*\n")), settings)
    assert len(runs_on(workspace, settings)) == 1


def test_a_missing_changed_paths_file_matches_every_workspace(settings, bind, state_machine):
    """No list means nothing can be ruled out."""
    workspace = bind(trigger_patterns=["nowhere/**"])
    deliver(upload(settings, claims(), tarball(None)), settings)
    assert len(runs_on(workspace, settings)) == 1


def test_a_workspace_with_no_run_role_is_skipped(settings, bind, state_machine, reported):
    """It is skipped rather than failing the other workspaces, and the started run carries the skip."""
    ready = bind("ready")
    roleless = bind("roleless", run_role_arn=None)
    deliver(upload(settings, claims(), tarball()), settings)
    [run] = runs_on(ready, settings)
    assert runs_on(roleless, settings) == []
    assert run["vcs"]["skipped"] == [
        {"workspace_id": roleless["workspace_id"], "workspace_name": "roleless", "reason": ingest.NO_RUN_ROLE_REASON}
    ]
    assert reported == []


def test_an_upload_matching_nothing_is_reported_as_no_runs_needed(settings, bind, state_machine, reported):
    """No bound workspace matches the changed paths, so the consumer reports the upload itself."""
    bind(trigger_patterns=["stacks/**"])
    event = upload(settings, pr_claims(4), tarball("docs/readme.md\n"))
    assert deliver(event, settings) == []
    [(upload_id, skipped)] = reported
    assert event["key"].endswith(f"{upload_id}.tar.gz")
    assert skipped == []


def test_a_push_no_workspace_tracks_is_reported(settings, bind, state_machine, reported):
    """A push to an untracked branch still gets its aggregate."""
    bind()
    assert deliver(upload(settings, claims(ref="refs/heads/feature"), tarball()), settings) == []
    [(_, skipped)] = reported
    assert skipped == []


def test_an_upload_whose_only_match_cannot_run_reports_the_skip(settings, bind, state_machine, reported):
    """A matched workspace with no run role is reported by name and reason."""
    roleless = bind("roleless", run_role_arn=None)
    assert deliver(upload(settings, pr_claims(4), tarball()), settings) == []
    [(_, skipped)] = reported
    assert skipped == [
        {"workspace_id": roleless["workspace_id"], "workspace_name": "roleless", "reason": ingest.NO_RUN_ROLE_REASON}
    ]


def test_a_run_role_that_is_not_a_role_arn_is_skipped(settings, bind, state_machine, reported):
    """A malformed role cannot be assumed, so the workspace is skipped before any run."""
    broken = bind("broken", run_role_arn="arn:aws:s3:::not-a-role-at-all")
    assert deliver(upload(settings, claims(), tarball()), settings) == []
    assert runs_on(broken, settings) == []
    [(_, skipped)] = reported
    assert [item["reason"] for item in skipped] == [ingest.BAD_RUN_ROLE_REASON]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (None, ingest.NO_RUN_ROLE_REASON),
        ("", ingest.NO_RUN_ROLE_REASON),
        ("arn:aws:iam::870550636948:user/someone", ingest.BAD_RUN_ROLE_REASON),
        ("not an arn at all, just words", ingest.BAD_RUN_ROLE_REASON),
        (ROLE, None),
        ("arn:aws:iam::870550636948:role/path/to/role-name", None),
    ],
)
def test_skip_reason(role, expected):
    """A missing role and one that is not an IAM role ARN are skips."""
    assert ingest.skip_reason({"run_role_arn": role}) == expected


def test_an_upload_that_starts_a_run_is_left_to_the_run_reports(settings, bind, state_machine, reported):
    """Runs report themselves through the stream, so the consumer posts nothing."""
    bind()
    assert len(deliver(upload(settings, claims(), tarball()), settings)) == 1
    assert reported == []


def test_a_superseded_upload_is_not_reported(settings, bind, state_machine, reported):
    """An older upload a newer one replaced reports nothing on its stale commit."""
    bind()
    older = upload(settings, claims(run_id="1"), tarball())
    newer = upload(settings, claims(run_id="2", sha="d" * 40), tarball())
    deliver(newer, settings)
    assert deliver(older, settings) == []
    assert reported == []


def test_a_redelivery_creates_nothing_new(settings, bind, state_machine):
    """Delivery is at least once: the same message twice is one run."""
    workspace = bind()
    event = upload(settings, claims(), tarball())
    first = deliver(event, settings)
    second = deliver(event, settings)
    assert first == second
    assert len(runs_on(workspace, settings)) == 1
    assert len(runs_service.list_runs(workspace["workspace_id"], settings=settings)) == 1


def test_a_retried_put_firing_twice_is_one_run(settings, bind, state_machine):
    """curl retries the PUT, so S3 can emit two events for one key. One run results."""
    workspace = bind()
    data = tarball()
    event = upload(settings, claims(), data)
    boto3.client("s3", region_name="us-west-2").put_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=event["key"], Body=data
    )
    deliver(event, settings)
    deliver(dict(event), settings)
    runs = runs_on(workspace, settings)
    assert len(runs) == 1
    assert len(workspaces_service.list_config_versions(workspace["workspace_id"], settings=settings)) == 1


def test_a_run_stored_but_never_started_is_started_on_redelivery(settings, bind, state_machine, monkeypatch):
    """A crash between the put and the start is healed by the retry."""
    workspace = bind()
    event = upload(settings, claims(), tarball())
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


def test_the_consumer_trusts_the_record_not_the_object(settings, bind, state_machine):
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


def test_a_foreign_bucket_is_dropped(settings, bind, state_machine):
    """The rule filters on the artifacts bucket; anything else is not ours."""
    bind()
    event = upload(settings, claims(), tarball()) | {"bucket": "someone-else"}
    assert deliver(event, settings) == []


def test_a_size_the_record_does_not_declare_is_dropped(settings, bind, state_machine):
    """The object has to be the one the record signed for."""
    workspace = bind()
    event = upload(settings, claims(), tarball()) | {"size": 1}
    assert deliver(event, settings) == []
    assert runs_on(workspace, settings) == []


def test_a_newer_push_cancels_an_older_pending_one(settings, bind, state_machine):
    """Supersede: an older queued push run on the same branch is cancelled."""
    workspace = bind()
    first = deliver(upload(settings, claims(run_id="1"), tarball()), settings)[0]
    second = deliver(upload(settings, claims(run_id="2", sha="d" * 40), tarball()), settings)[0]
    third = deliver(upload(settings, claims(run_id="3", sha="e" * 40), tarball()), settings)[0]
    assert runs_service.get_run(first, settings=settings)["status"] == "planning"
    assert runs_service.get_run(second, settings=settings)["status"] == "cancelled"
    assert runs_service.get_run(third, settings=settings)["status"] == "pending"
    assert len(runs_on(workspace, settings)) == 3


def test_a_newer_push_discards_an_older_run_awaiting_confirmation(settings, bind, state_machine):
    """Supersede: an older plan waiting on a person is discarded."""
    bind()
    first = deliver(upload(settings, claims(run_id="1"), tarball()), settings)[0]
    repositories.runs(settings).update(
        {"run_id": first},
        update_expression="SET #s = :s",
        expression_names={"#s": "status"},
        expression_values={":s": "awaiting_confirmation"},
    )
    deliver(upload(settings, claims(run_id="2", sha="d" * 40), tarball()), settings)
    assert runs_service.get_run(first, settings=settings)["status"] == "discarded"


def test_a_pull_request_supersedes_only_its_own_number(settings, bind, state_machine):
    """Another pull request's queued plan is left alone."""
    bind()
    deliver(upload(settings, claims(run_id="1"), tarball()), settings)
    other = deliver(upload(settings, pr_claims(5, run_id="2"), tarball()), settings)[0]
    older = deliver(upload(settings, pr_claims(6, run_id="3"), tarball()), settings)[0]
    newer = deliver(upload(settings, pr_claims(6, run_id="4", sha="f" * 40), tarball()), settings)[0]
    assert runs_service.get_run(other, settings=settings)["status"] == "pending"
    assert runs_service.get_run(older, settings=settings)["status"] == "cancelled"
    assert runs_service.get_run(newer, settings=settings)["status"] == "pending"


NEWER_HEAD = "9" * 40


def test_a_newer_commit_cancels_and_marks_a_pull_request_plan_mid_plan(settings, bind, state_machine):
    """HCP Terraform's behaviour: an outdated commit's plan still planning is cancelled and marked."""
    bind()
    older = deliver(upload(settings, pr_claims(6, run_id="1"), tarball()), settings)[0]
    assert runs_service.get_run(older, settings=settings)["status"] == "planning"
    newer = deliver(upload(settings, pr_claims(6, run_id="2", sha="f" * 40), tarball(), sha=NEWER_HEAD), settings)[0]
    run = runs_service.get_run(older, settings=settings)
    assert run["status"] == "cancelled"
    assert run["superseded_by"] == {"run_id": newer, "sha": NEWER_HEAD}
    assert runs_service.get_run(newer, settings=settings)["status"] in ("pending", "planning")
    assert "superseded_by" not in runs_service.get_run(newer, settings=settings)


def test_a_newer_commit_marks_a_finished_pull_request_plan_and_leaves_its_status(settings, bind, state_machine):
    """A plan that already finished keeps its result and only gains the marker."""
    bind()
    older = deliver(upload(settings, pr_claims(6, run_id="1"), tarball()), settings)[0]
    repositories.runs(settings).update(
        {"run_id": older},
        update_expression="SET #s = :s",
        expression_names={"#s": "status"},
        expression_values={":s": "planned_and_finished"},
    )
    newer = deliver(upload(settings, pr_claims(6, run_id="2", sha="f" * 40), tarball(), sha=NEWER_HEAD), settings)[0]
    run = runs_service.get_run(older, settings=settings)
    assert run["status"] == "planned_and_finished"
    assert run["superseded_by"] == {"run_id": newer, "sha": NEWER_HEAD}


def test_the_first_newer_commit_to_mark_a_plan_stands(settings, bind, state_machine):
    """A third commit marks only the second commit's plan, not the first again."""
    bind()
    first = deliver(upload(settings, pr_claims(6, run_id="1"), tarball()), settings)[0]
    second = deliver(upload(settings, pr_claims(6, run_id="2", sha="f" * 40), tarball(), sha=NEWER_HEAD), settings)[0]
    third = deliver(upload(settings, pr_claims(6, run_id="3", sha="e" * 40), tarball(), sha="8" * 40), settings)[0]
    assert runs_service.get_run(first, settings=settings)["superseded_by"]["run_id"] == second
    assert runs_service.get_run(second, settings=settings)["superseded_by"]["run_id"] == third


def test_one_workspace_never_supersedes_another(settings, bind, state_machine):
    """A newer commit that runs on one workspace leaves the other workspace's plan running and unmarked."""
    one = bind("one", working_directory="stacks/one")
    two = bind("two", working_directory="stacks/two")
    both = tarball("stacks/one/main.tf\nstacks/two/main.tf\n")
    deliver(upload(settings, pr_claims(6, run_id="1"), both), settings)
    [older_one] = runs_on(one, settings)
    [older_two] = runs_on(two, settings)
    [newer] = deliver(
        upload(settings, pr_claims(6, run_id="2", sha="f" * 40), tarball("stacks/one/main.tf\n"), sha=NEWER_HEAD),
        settings,
    )
    assert runs_service.get_run(newer, settings=settings)["workspace_id"] == one["workspace_id"]
    marked = runs_service.get_run(older_one["run_id"], settings=settings)
    assert (marked["status"], marked["superseded_by"]["run_id"]) == ("cancelled", newer)
    untouched = runs_service.get_run(older_two["run_id"], settings=settings)
    assert untouched["status"] == older_two["status"]
    assert "superseded_by" not in untouched


def test_push_runs_are_never_marked_superseded(settings, bind, state_machine):
    """Tracked branch runs keep the push rules, and a pull request never touches them."""
    bind()
    push = deliver(upload(settings, claims(run_id="1"), tarball()), settings)[0]
    repositories.runs(settings).update(
        {"run_id": push},
        update_expression="SET #s = :s",
        expression_names={"#s": "status"},
        expression_values={":s": "awaiting_confirmation"},
    )
    deliver(upload(settings, pr_claims(6, run_id="2"), tarball()), settings)
    held = runs_service.get_run(push, settings=settings)
    assert held["status"] == "awaiting_confirmation"
    assert "superseded_by" not in held
    deliver(upload(settings, claims(run_id="3", sha="d" * 40), tarball()), settings)
    assert "superseded_by" not in runs_service.get_run(push, settings=settings)


def test_a_push_does_not_supersede_an_api_run(settings, bind, auth_client, state_machine):
    """Only runs from the same source are replaced."""
    workspace = bind()
    deliver(upload(settings, claims(run_id="1"), tarball()), settings)
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
    deliver(upload(settings, claims(run_id="2", sha="d" * 40), tarball()), settings)
    assert runs_service.get_run(api_run["run_id"], settings=settings)["status"] == "pending"
    assert runs_service.get_run(running["run_id"], settings=settings)["status"] == "planning"


def test_a_late_older_upload_is_not_run_after_a_newer_one(settings, bind, state_machine):
    """Delivered out of order, the older upload starts nothing."""
    workspace = bind()
    older = upload(settings, claims(run_id="1"), tarball())
    newer = upload(settings, claims(run_id="2", sha="d" * 40), tarball())
    deliver(newer, settings)
    assert deliver(older, settings) == []
    assert len(runs_on(workspace, settings)) == 1


def test_one_upload_runs_on_every_bound_workspace(settings, bind, state_machine):
    """Two workspaces on one repository each get their own run."""
    first = bind("first")
    second = bind("second")
    run_ids = deliver(upload(settings, claims(), tarball()), settings)
    assert len(run_ids) == 2
    assert len(runs_on(first, settings)) == 1
    assert len(runs_on(second, settings)) == 1


def test_the_events_route_dispatches_an_ingest(settings, bind, state_machine):
    """The adapter's pass-through path routes `config_ingested` to this consumer."""
    workspace = bind()
    event = upload(settings, claims(), tarball())
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


def test_the_config_version_carries_the_readme_and_the_commit(settings, bind, state_machine):
    """The README is read once at ingest, so the overview never opens the tarball."""
    bind("app", working_directory="stacks/app")
    data = tarball("stacks/app/main.tf\n", {"README.md": "# Root\n", "stacks/app/README.md": "# App\n"})
    [run_id] = deliver(upload(settings, claims(), data), settings)
    run = runs_service.get_run(run_id, settings=settings)
    row = repositories.config_versions(settings).get({"config_version_id": run["config_version_id"]}) or {}
    assert row["readme_scanned"] is True
    assert row["readme"]["path"] == "stacks/app/README.md"
    assert row["readme"]["content"] == "# App\n"
    assert row["vcs"]["repo"] == REPO
    assert row["vcs"]["sha"] == HEAD_SHA
    assert row["vcs"]["branch"] == "main"
    assert row["vcs"]["pr_number"] is None


def test_a_pull_request_config_version_records_its_number(settings, bind, state_machine):
    """A speculative config version is marked so the overview can pass over it."""
    bind()
    [run_id] = deliver(upload(settings, pr_claims(), tarball()), settings)
    run = runs_service.get_run(run_id, settings=settings)
    row = repositories.config_versions(settings).get({"config_version_id": run["config_version_id"]}) or {}
    assert row["vcs"]["pr_number"] == run["vcs"]["pr_number"]
    assert row["readme_scanned"] is True
    assert "readme" not in row


def test_a_readme_read_failure_does_not_hold_up_the_run(settings, bind, state_machine, monkeypatch):
    """The README is best effort: the run starts and the row is left for a later read."""
    from app.common.workspaces import readme

    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(readme, "read_config_readme", fail)
    bind()
    [run_id] = deliver(upload(settings, claims(), tarball()), settings)
    run = runs_service.get_run(run_id, settings=settings)
    row = repositories.config_versions(settings).get({"config_version_id": run["config_version_id"]}) or {}
    assert row["status"] == "uploaded"
    assert "readme_scanned" not in row
