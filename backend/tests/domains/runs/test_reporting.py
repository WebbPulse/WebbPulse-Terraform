"""Reporting VCS runs to GitHub: check runs, the aggregate check and the PR comment.

GitHub is replaced at the HTTP layer with `httpx.MockTransport`, so the real App
client signs a real App JWT, exchanges it for an installation token and sends the
real check run and comment calls. Runs and workspaces are written straight into
moto's tables, since what is under test is what a stored run reports, not how it
got there.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from boto3.dynamodb.types import TypeSerializer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from vcs_helpers import BASE_SHA, HEAD_SHA, MERGE_SHA, REPO, REPOSITORY_ID

from app.common.composition import settings as settings_module
from app.common.db import repositories
from app.common.github import loader
from app.domains.runs import reporting
from app.domains.runs.consumers import dispatch, reports

APP_ID = 5150
INSTALLATION_ID = 777
FRONTEND = "https://staging.terraform.example.com"
PUSH_SHA = "d" * 40
OTHER_SHA = "e" * 40
CHECK = reporting.CHECK_NAME


@dataclass
class FakeGitHub:
    """The calls reporting makes, answered from plain dictionaries."""

    parents: dict[str, list[str]] = field(default_factory=dict)
    pull_heads: dict[int, str] = field(default_factory=dict)
    pull_commits: dict[int, list[str]] = field(default_factory=dict)
    compare: dict[str, str] = field(default_factory=dict)
    check_runs: list[dict[str, Any]] = field(default_factory=list)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    pulls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failure: int | None = None
    retire_failure: int | None = None
    """Answers a PATCH that only rewrites a completed check's output with this status."""

    def writes(self) -> list[tuple[str, str]]:
        """Every non-GET call against a repository, as method and path."""
        return [
            (request.method, request.url.path)
            for request in self.requests
            if request.method != "GET" and request.url.path.startswith("/repos/")
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request as GitHub would."""
        self.requests.append(request)
        if self.failure is not None:
            return httpx.Response(self.failure, json={"message": "failed"})
        path = request.url.path
        base = f"/repos/{REPO}"
        if re.fullmatch(r"/app/installations/\d+/access_tokens", path):
            return httpx.Response(201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"{base}/installation":
            return httpx.Response(200, json={"id": INSTALLATION_ID})
        if match := re.fullmatch(rf"{base}/compare/(.+)\.\.\.(\w+)", path):
            status = self.compare.get(match.group(2))
            return httpx.Response(200, json={"status": status}) if status else httpx.Response(404, json={})
        if match := re.fullmatch(rf"{base}/commits/(\w+)/check-runs", path):
            name = request.url.params.get("check_name")
            found = [run for run in self.check_runs if run["head_sha"] == match.group(1) and run["name"] == name]
            return httpx.Response(200, json={"total_count": len(found), "check_runs": found})
        if match := re.fullmatch(rf"{base}/commits/(\w+)/pulls", path):
            listed = self.pulls.get(match.group(1))
            return httpx.Response(200, json=listed) if listed is not None else httpx.Response(404, json={})
        if match := re.fullmatch(rf"{base}/commits/(\w+)", path):
            sha = match.group(1)
            parents = self.parents.get(sha)
            if parents is None and sha not in self.messages:
                return httpx.Response(404, json={})
            return httpx.Response(
                200,
                json={
                    "sha": sha,
                    "parents": [{"sha": parent} for parent in parents or []],
                    "commit": {"message": self.messages.get(sha, "Merge")},
                },
            )
        if match := re.fullmatch(rf"{base}/pulls/(\d+)/commits", path):
            return httpx.Response(200, json=[{"sha": sha} for sha in self.pull_commits.get(int(match.group(1)), [])])
        if match := re.fullmatch(rf"{base}/pulls/(\d+)", path):
            head = self.pull_heads.get(int(match.group(1)))
            return httpx.Response(200, json={"head": {"sha": head}}) if head else httpx.Response(404, json={})
        if path == f"{base}/check-runs" and request.method == "POST":
            body = json.loads(request.content)
            created = {"id": len(self.check_runs) + 1, "app": {"id": APP_ID}, "conclusion": None} | body
            self.check_runs.append(created)
            return httpx.Response(201, json=created)
        if (match := re.fullmatch(rf"{base}/check-runs/(\d+)", path)) and request.method == "PATCH":
            [found] = [run for run in self.check_runs if run["id"] == int(match.group(1))]
            body = json.loads(request.content)
            if found["status"] == "completed":
                if self.retire_failure is not None and set(body) == {"output"}:
                    return httpx.Response(self.retire_failure, json={"message": "failed"})
                body = {key: value for key, value in body.items() if key not in ("status", "conclusion")}
            found.update(body)
            return httpx.Response(200, json=found)
        if match := re.fullmatch(rf"{base}/issues/(\d+)/comments", path):
            listed = self.comments.setdefault(int(match.group(1)), [])
            if request.method == "GET":
                return httpx.Response(200, json=listed)
            created = {"id": 900 + len(listed), "user": {"type": "Bot"}, "html_url": "https://x"}
            created |= json.loads(request.content)
            listed.append(created)
            return httpx.Response(201, json=created)
        if (match := re.fullmatch(rf"{base}/issues/comments/(\d+)", path)) and request.method == "PATCH":
            for listed in self.comments.values():
                for comment in listed:
                    if comment["id"] == int(match.group(1)):
                        comment.update(json.loads(request.content))
                        return httpx.Response(200, json=comment)
        return httpx.Response(404, json={"message": "Not Found"})

    def named(self, name: str) -> list[dict[str, Any]]:
        """The check runs with this name, oldest first."""
        return [run for run in self.check_runs if run["name"] == name]


@pytest.fixture(scope="session")
def private_key_pem() -> str:
    """One RSA key for the whole session."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch, private_key_pem: str) -> Iterator[FakeGitHub]:
    """A configured App talking to a fake GitHub."""
    fake = FakeGitHub()
    monkeypatch.setattr(reporting, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handle)))
    monkeypatch.setenv("GITHUB_APP_ID", str(APP_ID))
    monkeypatch.setenv("GITHUB_PRIVATE_KEY", private_key_pem)
    monkeypatch.setenv("IDENTITY_FRONTEND_BASE_URL", FRONTEND)
    settings_module.reset_settings_cache()
    loader.invalidate()
    yield fake
    loader.invalidate()
    settings_module.reset_settings_cache()


def bind(settings: Any, workspace_id: str, name: str) -> None:
    """A workspace bound to the test repository by id."""
    repositories.workspaces(settings).put(
        {
            "workspace_id": workspace_id,
            "name": name,
            "vcs_repo": REPO,
            "vcs_repo_key": REPO.lower(),
            "vcs_repository_id": REPOSITORY_ID,
        }
    )


def store_run(settings: Any, run_id: str, workspace_id: str, status: str, **fields: Any) -> dict[str, Any]:
    """Write one run row, a push run unless `source` says otherwise."""
    source = fields.pop("source", "vcs_push")
    if source == "vcs_pr":
        vcs = {
            "repo": REPO,
            "repository_id": REPOSITORY_ID,
            "sha": MERGE_SHA,
            "ref": "refs/pull/7/merge",
            "pr_number": 7,
            "head_sha": fields.pop("head_sha", HEAD_SHA),
            "base_sha": BASE_SHA,
        }
    else:
        vcs = {
            "repo": REPO,
            "repository_id": REPOSITORY_ID,
            "sha": PUSH_SHA,
            "ref": "refs/heads/main",
            "branch": "main",
        }
    item = {
        "run_id": run_id,
        "workspace_id": workspace_id,
        "status": status,
        "plan_only": source == "vcs_pr",
        "source": source,
        "vcs": vcs,
        "message": "Push of ddddddd to main",
        "created_at": fields.pop("created_at", f"2026-09-26T00:00:0{run_id[-1]}Z"),
    } | fields
    repositories.runs(settings).put(item)
    return item


def set_status(settings: Any, run_id: str, status: str, **fields: Any) -> None:
    """Move a stored run to `status`."""
    row = repositories.runs(settings).get({"run_id": run_id}) or {}
    repositories.runs(settings).put(dict(row) | {"status": status} | fields)


@pytest.fixture
def push_ready(github: FakeGitHub, settings: Any) -> FakeGitHub:
    """One bound workspace, and a pushed commit the branch contains."""
    github.compare[PUSH_SHA] = "identical"
    bind(settings, "ws-1", "network")
    return github


@pytest.fixture
def pr_ready(github: FakeGitHub, settings: Any) -> FakeGitHub:
    """One bound workspace, and a pull request whose merge commit's head is `HEAD_SHA`."""
    github.parents[MERGE_SHA] = [BASE_SHA, HEAD_SHA]
    github.pull_heads[7] = HEAD_SHA
    github.pull_commits[7] = [HEAD_SHA]
    bind(settings, "ws-1", "network")
    return github


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("pending", ("queued", None, "Run queued")),
        ("planning", ("in_progress", None, "Planning")),
        ("planned", ("in_progress", None, "Planned")),
        ("applying", ("in_progress", None, "Applying")),
        ("awaiting_confirmation", ("completed", "action_required", "Run pending confirmation")),
        ("applied", ("completed", "success", "Applied")),
        ("planned_and_finished", ("completed", "success", "Planned and finished")),
        ("errored", ("completed", "failure", "Run errored")),
        ("cancelled", ("completed", "cancelled", "Run cancelled")),
        ("discarded", ("completed", "neutral", "Run discarded")),
    ],
)
def test_each_status_maps_to_a_check_state(status, expected):
    """The check run status, conclusion and title for every run status."""
    state = reporting.check_state({"status": status})
    assert (state.status, state.conclusion, state.title) == expected


def test_counts_are_worded_like_hcp_terraform():
    """Planned counts, no changes, and an apply's own counts once applied."""
    assert reporting.counts_line({"status": "planning"}) == "Plan not finished yet."
    planned = {"status": "planned_and_finished", "changes": {"add": 2, "change": 1, "destroy": 0}}
    assert reporting.counts_line(planned) == "Terraform plan: 2 to add, 1 to change, 0 to destroy."
    assert reporting.counts_line({"status": "planned", "changes": {"add": 0}}) == "Terraform plan: no changes."
    applied = planned | {"status": "applied", "apply_changes": {"add": 2, "change": 1, "destroy": 0}}
    assert reporting.counts_line(applied) == "Apply: 2 added, 1 changed, 0 destroyed."


def test_a_queued_push_run_creates_its_check_and_the_aggregate(push_ready, settings):
    """Both land on the pushed commit, the workspace check linking to the SPA run page."""
    store_run(settings, "run-1", "ws-1", "pending")
    assert reporting.report_run("run-1", settings=settings) is True

    [own] = push_ready.named("webbpulse-terraform/network")
    assert own["head_sha"] == PUSH_SHA
    assert own["status"] == "queued"
    assert own["external_id"] == "run-1"
    assert own["details_url"] == f"{FRONTEND}/workspaces/ws-1/runs/run-1"
    assert own["output"]["title"] == "Run queued"
    [overall] = push_ready.named("webbpulse-terraform")
    assert overall["status"] == "in_progress"
    assert "| network |" in overall["output"]["summary"]


def test_each_transition_updates_the_same_check(push_ready, settings):
    """No second check run is created, and the apply's counts reach the summary."""
    store_run(settings, "run-1", "ws-1", "planning")
    reporting.report_run("run-1", settings=settings)
    set_status(settings, "run-1", "applied", apply_changes={"add": 1, "change": 0, "destroy": 0})
    reporting.report_run("run-1", settings=settings)

    [own] = push_ready.named("webbpulse-terraform/network")
    assert (own["status"], own["conclusion"]) == ("completed", "success")
    assert own["output"]["summary"].startswith("Apply: 1 added, 0 changed, 0 destroyed.")
    [overall] = push_ready.named("webbpulse-terraform")
    assert (overall["status"], overall["conclusion"]) == ("completed", "success")


def test_a_held_push_run_asks_for_action_then_a_new_check_carries_the_apply(push_ready, settings, caplog):
    """`action_required` while held; once confirmed, new checks of the same names show progress.

    GitHub keeps a completed check completed, so the held checks keep their
    conclusion, are retired with a pointer to their successors, and the apply's
    later transitions update the new checks in place.
    """
    caplog.set_level(logging.INFO, logger=reporting.__name__)
    store_run(settings, "run-1", "ws-1", "awaiting_confirmation", changes={"add": 3, "change": 0, "destroy": 1})
    reporting.report_run("run-1", settings=settings)
    [held] = push_ready.named("webbpulse-terraform/network")
    assert (held["status"], held["conclusion"]) == ("completed", "action_required")
    assert held["output"]["title"] == "Run pending confirmation"
    assert held["output"]["summary"] == "Terraform plan: 3 to add, 0 to change, 1 to destroy."
    [overall] = push_ready.named("webbpulse-terraform")
    assert overall["conclusion"] == "action_required"

    set_status(settings, "run-1", "applying")
    assert reporting.report_run("run-1", settings=settings) is True
    old, applying = push_ready.named("webbpulse-terraform/network")
    assert old["id"] == held["id"]
    assert (old["status"], old["conclusion"]) == ("completed", "action_required")
    assert old["output"]["title"] == reporting.SUPERSEDED.title
    assert (applying["status"], applying["conclusion"], applying["external_id"]) == ("in_progress", None, "run-1")
    assert applying["output"]["title"] == "Applying"
    old_overall, new_overall = push_ready.named("webbpulse-terraform")
    assert (old_overall["status"], old_overall["conclusion"]) == ("completed", "action_required")
    assert (new_overall["status"], new_overall["conclusion"]) == ("in_progress", None)
    replaced = [record for record in caplog.records if getattr(record, "event", "") == "runs.report.check_replaced"]
    assert {getattr(record, "check_name", "") for record in replaced} == {"webbpulse-terraform/network", CHECK}

    set_status(settings, "run-1", "applied", apply_changes={"add": 3, "change": 0, "destroy": 1})
    reporting.report_run("run-1", settings=settings)
    _, applied = push_ready.named("webbpulse-terraform/network")
    assert (applied["id"], applied["status"], applied["conclusion"]) == (applying["id"], "completed", "success")
    _, finished = push_ready.named("webbpulse-terraform")
    assert (finished["id"], finished["conclusion"]) == (new_overall["id"], "success")
    assert [method for method, path in push_ready.writes() if path.endswith("/check-runs")] == ["POST"] * 4


def test_a_retirement_github_refuses_still_reports(push_ready, settings):
    """The new check is what matters, so a refused retirement of the old one is dropped."""
    push_ready.retire_failure = 422
    store_run(settings, "run-1", "ws-1", "awaiting_confirmation", changes={"add": 1, "change": 0, "destroy": 0})
    reporting.report_run("run-1", settings=settings)
    set_status(settings, "run-1", "applying")
    assert reporting.report_run("run-1", settings=settings) is True
    held, applying = push_ready.named("webbpulse-terraform/network")
    assert held["output"]["title"] == "Run pending confirmation"
    assert applying["status"] == "in_progress"


def test_the_aggregate_sums_every_bound_workspace(push_ready, settings):
    """One errored workspace fails the aggregate, and both appear in its table."""
    bind(settings, "ws-2", "compute")
    store_run(settings, "run-1", "ws-1", "applied")
    store_run(settings, "run-2", "ws-2", "errored", error="boom")
    reporting.report_run("run-2", settings=settings)

    [overall] = push_ready.named("webbpulse-terraform")
    assert overall["conclusion"] == "failure"
    assert "| compute |" in overall["output"]["summary"]
    assert "| network |" in overall["output"]["summary"]
    [own] = push_ready.named("webbpulse-terraform/compute")
    assert "boom" in own["output"]["summary"]


def test_a_pull_request_run_reports_on_its_head_and_comments_once(pr_ready, settings):
    """The check lands on the head, and a second report edits the one comment."""
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    reporting.report_run("run-1", settings=settings)
    set_status(settings, "run-1", "planned_and_finished", changes={"add": 1, "change": 0, "destroy": 0})
    reporting.report_run("run-1", settings=settings)

    [own] = pr_ready.named("webbpulse-terraform/network")
    assert own["head_sha"] == HEAD_SHA
    assert own["conclusion"] == "success"
    [comment] = pr_ready.comments[7]
    assert comment["body"].startswith(reporting.COMMENT_MARKER)
    assert "Planned and finished" in comment["body"]
    assert "1 to add" in comment["body"]
    methods = [method for method, path in pr_ready.writes() if "/issues/" in path]
    assert methods == ["POST", "PATCH"]


def test_a_head_the_pull_request_has_moved_past_still_verifies(pr_ready, settings):
    """A head that is no longer the tip but is one of the pull request's commits."""
    pr_ready.pull_heads[7] = OTHER_SHA
    pr_ready.pull_commits[7] = [HEAD_SHA, OTHER_SHA]
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    assert reporting.report_run("run-1", settings=settings) is True
    assert pr_ready.named("webbpulse-terraform/network")[0]["head_sha"] == HEAD_SHA


def test_a_forged_head_posts_nothing(pr_ready, settings):
    """A head that is not a parent of the signed merge commit gets no report."""
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr", head_sha=OTHER_SHA)
    assert reporting.report_run("run-1", settings=settings) is False
    assert pr_ready.writes() == []


def test_a_head_outside_the_pull_request_posts_nothing(pr_ready, settings):
    """The merge commit checks out but the head is no commit of that pull request."""
    pr_ready.pull_heads[7] = OTHER_SHA
    pr_ready.pull_commits[7] = [OTHER_SHA]
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    assert reporting.report_run("run-1", settings=settings) is False
    assert pr_ready.writes() == []


def test_a_commit_off_the_pushed_branch_posts_nothing(push_ready, settings):
    """A branch that has diverged from the commit gets no report."""
    push_ready.compare[PUSH_SHA] = "diverged"
    store_run(settings, "run-1", "ws-1", "planning")
    assert reporting.report_run("run-1", settings=settings) is False
    assert push_ready.writes() == []


def test_a_human_comment_carrying_the_marker_is_left_alone(pr_ready, settings):
    """Only a bot's marked comment is edited."""
    pr_ready.comments[7] = [{"id": 5, "body": f"{reporting.COMMENT_MARKER} copied", "user": {"type": "User"}}]
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    reporting.report_run("run-1", settings=settings)
    human, ours = pr_ready.comments[7]
    assert human["body"].endswith("copied")
    assert ours["user"]["type"] == "Bot"


def test_a_github_failure_is_swallowed(push_ready, settings):
    """A GitHub error is logged and the run is untouched."""
    push_ready.failure = 500
    store_run(settings, "run-1", "ws-1", "planning")
    assert reporting.report_run("run-1", settings=settings) is False
    assert (repositories.runs(settings).get({"run_id": "run-1"}) or {})["status"] == "planning"


def test_no_app_reports_nothing(settings, monkeypatch):
    """Without App credentials nothing is attempted."""
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    loader.invalidate()
    calls: list[Any] = []
    monkeypatch.setattr(reporting, "http_client", lambda: calls.append(1))
    bind(settings, "ws-1", "network")
    store_run(settings, "run-1", "ws-1", "planning")
    assert reporting.report_run("run-1", settings=settings) is False
    assert calls == []
    loader.invalidate()


def test_an_api_run_is_not_reported(push_ready, settings):
    """A run not started from VCS never reaches GitHub."""
    store_run(settings, "run-1", "ws-1", "planning", source="api")
    assert reporting.report_run("run-1", settings=settings) is False
    assert push_ready.requests == []


def stream_record(new: dict[str, Any], old: dict[str, Any] | None = None, event: str = "MODIFY") -> dict[str, Any]:
    """A DynamoDB Streams record for a runs table change."""
    serializer = TypeSerializer()
    images = {"NewImage": {key: serializer.serialize(value) for key, value in new.items()}}
    if old is not None:
        images["OldImage"] = {key: serializer.serialize(value) for key, value in old.items()}
    return {"eventID": "1", "eventName": event, "eventSource": "aws:dynamodb", "dynamodb": images}


def test_the_stream_reports_a_status_change(push_ready, settings):
    """A record whose status moved is reported through the events route's router."""
    run = store_run(settings, "run-1", "ws-1", "planning")
    record = stream_record(run, {**run, "status": "pending"})
    dispatch.route_record(record, settings=settings)
    assert push_ready.named("webbpulse-terraform/network")


def test_the_stream_reports_an_insert(push_ready, settings):
    """A new run is reported as soon as it is written."""
    run = store_run(settings, "run-1", "ws-1", "pending")
    assert reports.handle_record(stream_record(run, event="INSERT"), settings=settings) is True


def test_the_stream_drops_records_that_change_nothing_reportable(push_ready, settings):
    """An unchanged status, an API run and a removal are all acknowledged unreported."""
    run = store_run(settings, "run-1", "ws-1", "planning")
    assert reports.handle_record(stream_record(run, dict(run)), settings=settings) is False
    api = {**run, "source": "api"}
    assert reports.handle_record(stream_record(api, {**api, "status": "pending"}), settings=settings) is False
    assert reports.handle_record(stream_record(run, run, event="REMOVE"), settings=settings) is False
    assert push_ready.requests == []


def test_an_unreadable_stream_record_is_dropped(settings):
    """A malformed image is logged and acknowledged, never raised."""
    record = {
        "eventID": "1",
        "eventName": "MODIFY",
        "eventSource": "aws:dynamodb",
        "dynamodb": {"NewImage": {"source": {"ZZ": "x"}}},
    }
    assert reports.handle_record(record, settings=settings) is False


def test_the_stream_reports_queued_before_planning(pr_ready, settings):
    """An insert read back as `planning` still shows queued first on its check, from its own image.

    The comment lists every run of the pull request from the table, so it shows the
    newer state the table already holds.
    """
    run = store_run(settings, "run-1", "ws-1", "pending", source="vcs_pr")
    set_status(settings, "run-1", "planning")
    planning = {**run, "status": "planning"}
    reports.handle_record(stream_record(run, event="INSERT"), settings=settings)
    [own] = pr_ready.named("webbpulse-terraform/network")
    assert own["status"] == "queued"
    assert "Planning" in pr_ready.comments[7][0]["body"]

    reports.handle_record(stream_record(planning, run), settings=settings)
    [own] = pr_ready.named("webbpulse-terraform/network")
    assert own["status"] == "in_progress"
    assert "Planning" in pr_ready.comments[7][0]["body"]


def test_the_comment_and_aggregate_use_the_reported_state_not_a_lagging_index(pr_ready, settings):
    """A stale sibling row is replaced by the run being reported."""
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    finished = store_run(
        settings, "run-1", "ws-1", "planning", source="vcs_pr", changes={"add": 1, "change": 0, "destroy": 0}
    ) | {"status": "planned_and_finished"}
    assert reporting.report_run("run-1", image=finished, settings=settings) is True
    assert "Planned and finished" in pr_ready.comments[7][0]["body"]
    [overall] = pr_ready.named("webbpulse-terraform")
    assert overall["conclusion"] == "success"


SECOND_MERGE_SHA = "f" * 40


def store_second(settings: Any, run_id: str, workspace_id: str, status: str, **fields: Any) -> dict[str, Any]:
    """A pull request run on a second pushed commit, `OTHER_SHA`, merged as `SECOND_MERGE_SHA`."""
    run = store_run(settings, run_id, workspace_id, status, source="vcs_pr", head_sha=OTHER_SHA, **fields)
    vcs = dict(run["vcs"]) | {"sha": SECOND_MERGE_SHA}
    set_status(settings, run_id, status, vcs=vcs)
    return run | {"vcs": vcs}


def push_second(github: FakeGitHub) -> None:
    """The pull request moves on to `OTHER_SHA`, keeping `HEAD_SHA` as one of its commits."""
    github.parents[SECOND_MERGE_SHA] = [BASE_SHA, OTHER_SHA]
    github.pull_heads[7] = OTHER_SHA
    github.pull_commits[7] = [HEAD_SHA, OTHER_SHA]


def heading(body: str) -> str:
    """The comment's heading line."""
    return next(line for line in body.splitlines() if line.startswith("### "))


def test_two_quick_pushes_keep_one_current_comment(pr_ready, settings):
    """The first commit's runs finish after the second commit's comment exists.

    The comment stays on the newer commit, the older commit's runs move into the
    collapsed section and show finished there, and nothing settled shows queued
    or planning.
    """
    bind(settings, "ws-2", "compute")
    first = [
        store_run(settings, "run-1", "ws-1", "pending", source="vcs_pr"),
        store_run(settings, "run-2", "ws-2", "pending", source="vcs_pr"),
    ]
    for run in first:
        reports.handle_record(stream_record(run, event="INSERT"), settings=settings)
    [comment] = pr_ready.comments[7]
    assert heading(comment["body"]) == f"### WebbPulse Terraform runs for {HEAD_SHA[:7]}"

    push_second(pr_ready)
    second = [
        store_second(settings, "run-3", "ws-1", "pending"),
        store_second(settings, "run-4", "ws-2", "pending"),
    ]
    for run in second:
        reports.handle_record(stream_record(run, event="INSERT"), settings=settings)
    [comment] = pr_ready.comments[7]
    assert heading(comment["body"]) == f"### WebbPulse Terraform runs for {OTHER_SHA[:7]}"

    for run in first:
        set_status(settings, run["run_id"], "planned_and_finished", changes={"add": 1, "change": 0, "destroy": 0})
        finished = {**run, "status": "planned_and_finished", "changes": {"add": 1, "change": 0, "destroy": 0}}
        reports.handle_record(stream_record(finished, run), settings=settings)

    [comment] = pr_ready.comments[7]
    body = comment["body"]
    assert heading(body) == f"### WebbPulse Terraform runs for {OTHER_SHA[:7]}"
    current, earlier = body.split("<details>")
    assert current.count("Run queued") == 2
    assert "<summary>Earlier commits (1)</summary>" in earlier
    assert f"#### {HEAD_SHA[:7]}" in earlier
    assert earlier.count("Planned and finished") == 2
    assert "Run queued" not in earlier and "Planning" not in earlier
    assert [method for method, path in pr_ready.writes() if "/issues/" in path][0] == "POST"
    assert [method for method, path in pr_ready.writes() if "/issues/" in path].count("POST") == 1


def test_a_late_report_with_a_stale_image_never_regresses_the_comment(pr_ready, settings):
    """A report carrying an older image than the table shows the table's newer state."""
    run = store_run(settings, "run-1", "ws-1", "pending", source="vcs_pr")
    set_status(settings, "run-1", "planned_and_finished", changes={"add": 0, "change": 0, "destroy": 0})
    assert reporting.report_run("run-1", image=run, settings=settings) is True
    body = pr_ready.comments[7][0]["body"]
    assert "Planned and finished" in body
    assert "Run queued" not in body


def test_racing_duplicates_are_folded_into_the_oldest_comment(pr_ready, settings):
    """Two marked comments left by an earlier race: the oldest is kept current, the other points at it."""
    stale = f"{reporting.COMMENT_MARKER}\n### WebbPulse Terraform runs for {HEAD_SHA[:7]}\n\n| network | Planning |"
    pr_ready.comments[7] = [
        {"id": 10, "body": stale, "user": {"type": "Bot"}, "html_url": "https://github.com/c/10"},
        {"id": 11, "body": stale, "user": {"type": "Bot"}, "html_url": "https://github.com/c/11"},
    ]
    store_run(settings, "run-1", "ws-1", "planned_and_finished", source="vcs_pr")
    assert reporting.report_run("run-1", settings=settings) is True
    kept, duplicate = pr_ready.comments[7]
    assert "Planned and finished" in kept["body"]
    assert duplicate["body"].startswith(reporting.COMMENT_MARKER)
    assert "https://github.com/c/10" in duplicate["body"]
    assert "Planning" not in duplicate["body"]
    assert [method for method, path in pr_ready.writes() if "/issues/" in path] == ["PATCH", "PATCH"]


def claim(settings: Any, claimed_at: int) -> None:
    """A creation claim on pull request 7, as another report would leave it."""
    repositories.runs(settings).put(
        {"run_id": f"{reporting.COMMENT_CLAIM_PREFIX}{REPO.lower()}#7", "claimed_at": claimed_at}
    )


def test_a_report_that_loses_the_claim_edits_the_winners_comment(pr_ready, settings, monkeypatch):
    """Only the claim holder posts; the other waits for its comment and brings it up to date."""
    claim(settings, int(time.time()))

    def winner_posts(seconds: float) -> None:
        """The claim holder's comment appears while this report waits."""
        if not pr_ready.comments.get(7):
            body = f"{reporting.COMMENT_MARKER}\nold"
            pr_ready.comments[7] = [{"id": 20, "body": body, "user": {"type": "Bot"}, "html_url": "https://x"}]

    monkeypatch.setattr(reporting, "pause", winner_posts)
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    assert reporting.report_run("run-1", settings=settings) is True
    [comment] = pr_ready.comments[7]
    assert "Planning" in comment["body"]
    assert [method for method, path in pr_ready.writes() if "/issues/" in path] == ["PATCH"]


def test_a_report_that_loses_the_claim_and_sees_no_comment_posts_nothing(pr_ready, settings, monkeypatch):
    """The claim holder's own report carries the run, so the loser gives up rather than posting a second."""
    claim(settings, int(time.time()))
    waits: list[float] = []
    monkeypatch.setattr(reporting, "pause", waits.append)
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    assert reporting.report_run("run-1", settings=settings) is True
    assert 7 not in pr_ready.comments or pr_ready.comments[7] == []
    assert sum(waits) == reporting.COMMENT_WAIT_SECONDS


def test_a_stale_claim_is_taken_over(pr_ready, settings):
    """A claim whose report died before posting does not block the comment for good."""
    claim(settings, int(time.time()) - reporting.COMMENT_CLAIM_SECONDS - 5)
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    assert reporting.report_run("run-1", settings=settings) is True
    [comment] = pr_ready.comments[7]
    assert "Planning" in comment["body"]


def test_a_superseded_plan_is_marked_in_its_check_and_the_comment(pr_ready, settings):
    """A plan a newer commit cancelled says so, and a finished one carries the newer commit."""
    bind(settings, "ws-2", "compute")
    push_second(pr_ready)
    by = {"run_id": "run-3", "sha": OTHER_SHA}
    store_run(settings, "run-1", "ws-1", "cancelled", source="vcs_pr", superseded_by=by)
    store_run(
        settings,
        "run-2",
        "ws-2",
        "planned_and_finished",
        source="vcs_pr",
        superseded_by={"run_id": "run-4", "sha": OTHER_SHA},
    )
    store_second(settings, "run-3", "ws-1", "planning")
    assert reporting.report_run("run-1", settings=settings) is True
    [own] = pr_ready.named("webbpulse-terraform/network")
    assert (own["head_sha"], own["conclusion"]) == (HEAD_SHA, "cancelled")
    assert own["output"]["title"] == reporting.SUPERSEDED_TITLE
    body = pr_ready.comments[7][0]["body"]
    assert heading(body) == f"### WebbPulse Terraform runs for {OTHER_SHA[:7]}"
    _, earlier = body.split("<details>")
    assert earlier.count(f"(superseded by {OTHER_SHA[:7]})") == 2


def test_the_stream_reports_a_run_newly_marked_superseded(pr_ready, settings):
    """The mark leaves the status alone, but the comment has to show it."""
    run = store_run(settings, "run-1", "ws-1", "planned_and_finished", source="vcs_pr")
    marked = {**run, "superseded_by": {"run_id": "run-3", "sha": OTHER_SHA}}
    assert reports.reportable_image(stream_record(marked, run)) is not None
    assert reports.reportable_image(stream_record(marked, marked)) is None


def test_earlier_commits_are_collapsed_newest_first_and_capped(settings):
    """Each earlier commit gets its own table under one collapsed section, the oldest beyond the cap left out."""
    runs = [
        {
            "run_id": f"run-{index:02d}",
            "workspace_id": "ws-1",
            "workspace_name": "network",
            "status": "planned_and_finished",
            "vcs": {"head_sha": f"{index:02d}" * 20},
        }
        for index in range(reporting.EARLIER_COMMITS_SHOWN + 3)
    ]
    head = runs[-1]["vcs"]["head_sha"]
    body = reporting.comment_body(runs, reporting.head_commit(runs), settings)
    assert heading(body) == f"### WebbPulse Terraform runs for {head[:7]}"
    shown = re.findall(r"^#### (\w+)$", body, flags=re.MULTILINE)
    assert shown == [(f"{index:02d}" * 20)[:7] for index in range(len(runs) - 2, 1, -1)]
    assert f"Earlier commits ({len(runs) - 1})" in body
    assert "2 older commits are not shown." in body


def test_with_current_keeps_a_newer_sibling_and_adds_a_missing_run():
    """The reported run replaces older rows of its workspace, never a newer one."""
    older = {"run_id": "run-1", "workspace_id": "ws-1", "status": "planning", "workspace_name": "network"}
    newer = {"run_id": "run-3", "workspace_id": "ws-1", "status": "pending", "workspace_name": "network"}
    other = {"run_id": "run-0", "workspace_id": "ws-2", "status": "applied", "workspace_name": "compute"}
    current = {"run_id": "run-2", "workspace_id": "ws-1", "status": "applied"}
    assert reporting.with_current([older, other], current, "network") == [
        other,
        current | {"workspace_name": "network"},
    ]
    assert reporting.with_current([newer], current, "network") == [newer]
    assert reporting.with_current([], current, "network") == [current | {"workspace_name": "network"}]


def test_the_head_commit_message_is_recorded_once(pr_ready, settings):
    """A pull request run keeps its head commit's message, read through the App once."""
    pr_ready.messages[HEAD_SHA] = "Add the bucket\n\nWith versioning."
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    reporting.report_run("run-1", settings=settings)
    stored = repositories.runs(settings).get({"run_id": "run-1"}) or {}
    assert stored["vcs"]["commit_message"] == "Add the bucket\n\nWith versioning."
    assert stored["status"] == "planning"

    reads = len([r for r in pr_ready.requests if r.url.path.endswith(f"/commits/{HEAD_SHA}")])
    reporting.report_run("run-1", settings=settings)
    assert len([r for r in pr_ready.requests if r.url.path.endswith(f"/commits/{HEAD_SHA}")]) == reads


def test_a_push_commit_message_is_recorded(push_ready, settings):
    """A push run keeps the pushed commit's message."""
    push_ready.messages[PUSH_SHA] = "Tighten the policy"
    store_run(settings, "run-1", "ws-1", "planning")
    reporting.report_run("run-1", settings=settings)
    stored = repositories.runs(settings).get({"run_id": "run-1"}) or {}
    assert stored["vcs"]["commit_message"] == "Tighten the policy"


def test_an_unreadable_commit_still_reports(push_ready, settings):
    """A commit GitHub will not answer for leaves no message but the check still lands."""
    store_run(settings, "run-1", "ws-1", "planning")
    assert reporting.report_run("run-1", settings=settings) is True
    stored = repositories.runs(settings).get({"run_id": "run-1"}) or {}
    assert "commit_message" not in stored["vcs"]


def test_a_push_merged_from_a_pull_request_links_it(push_ready, settings):
    """The pull request whose merge commit is the pushed commit is kept on the run, once."""
    push_ready.pulls[PUSH_SHA] = [
        {"number": 3, "html_url": f"https://github.com/{REPO}/pull/3", "merged_at": None, "merge_commit_sha": "x"},
        {
            "number": 4,
            "html_url": f"https://github.com/{REPO}/pull/4",
            "merged_at": "2026-09-26T00:00:00Z",
            "merge_commit_sha": PUSH_SHA,
        },
    ]
    store_run(settings, "run-1", "ws-1", "planning")
    reporting.report_run("run-1", settings=settings)
    stored = repositories.runs(settings).get({"run_id": "run-1"}) or {}
    assert stored["vcs"]["pull_request"] == {"number": 4, "url": f"https://github.com/{REPO}/pull/4"}
    assert stored["status"] == "planning"

    reads = len([r for r in push_ready.requests if r.url.path.endswith("/pulls")])
    reporting.report_run("run-1", settings=settings)
    assert len([r for r in push_ready.requests if r.url.path.endswith("/pulls")]) == reads


@pytest.mark.parametrize("pulls", [None, []])
def test_a_direct_push_or_an_unreadable_listing_links_nothing(push_ready, settings, pulls):
    """No pull request, or a listing GitHub will not answer, leaves no link and still reports."""
    if pulls is not None:
        push_ready.pulls[PUSH_SHA] = pulls
    store_run(settings, "run-1", "ws-1", "planning")
    assert reporting.report_run("run-1", settings=settings) is True
    stored = repositories.runs(settings).get({"run_id": "run-1"}) or {}
    assert "pull_request" not in stored["vcs"]


def test_a_pull_request_run_does_not_look_up_pull_requests(pr_ready, settings):
    """A pull request run already names its pull request."""
    store_run(settings, "run-1", "ws-1", "planning", source="vcs_pr")
    reporting.report_run("run-1", settings=settings)
    assert not [r for r in pr_ready.requests if r.url.path.endswith("/pulls")]


SKIP = {"workspace_name": "roleless", "reason": "The workspace has no run role."}


def pr_upload(**fields: Any) -> dict[str, Any]:
    """An ingest record for a pull request upload."""
    return {
        "upload_id": "up-1",
        "event": "pull_request",
        "repo": REPO,
        "repository_id": REPOSITORY_ID,
        "sha": MERGE_SHA,
        "ref": "refs/pull/7/merge",
        "pr_number": 7,
        "head_sha": HEAD_SHA,
    } | fields


def push_upload() -> dict[str, Any]:
    """An ingest record for a push upload."""
    return {
        "upload_id": "up-2",
        "event": "push",
        "repo": REPO,
        "repository_id": REPOSITORY_ID,
        "sha": PUSH_SHA,
        "ref": "refs/heads/main",
        "branch": "main",
    }


@pytest.mark.parametrize(
    ("statuses", "skipped", "expected"),
    [
        ([], [], ("completed", "success", "No runs needed")),
        ([], [SKIP], ("completed", "failure", "Workspace could not run")),
        (["applied"], [SKIP], ("completed", "failure", "Workspace could not run")),
        (["planning"], [SKIP], ("completed", "failure", "Workspace could not run")),
        (["planning", "applied"], [], ("in_progress", None, "Runs in progress")),
        (["applied", "errored"], [], ("completed", "failure", "Runs errored")),
        (["applied", "awaiting_confirmation"], [], ("completed", "action_required", "Runs pending confirmation")),
        (["planned_and_finished", "applied"], [], ("completed", "success", "All runs finished")),
        (["applied", "discarded"], [], ("completed", "neutral", "Runs finished")),
    ],
)
def test_the_aggregate_state(statuses, skipped, expected):
    """No runs pass, a skip fails outright, and runs keep their summed semantics."""
    state = reporting.aggregate_state([{"status": status} for status in statuses], skipped)
    assert (state.status, state.conclusion, state.title) == expected


def test_skipped_workspaces_are_read_off_the_runs_once_each():
    """Every run from one upload carries the same skips; they are listed once, by name."""
    runs = [
        {"vcs": {"skipped": [SKIP]}},
        {"vcs": {"skipped": [SKIP, {"workspace_name": "alpha", "reason": "r"}]}},
        {"vcs": {}},
    ]
    assert reporting.skipped_workspaces(runs) == [{"workspace_name": "alpha", "reason": "r"}, SKIP]


def test_an_upload_with_no_runs_needed_passes_the_aggregate(pr_ready, settings):
    """The pull request's head gets a successful aggregate and nothing else."""
    assert reporting.report_upload(pr_upload(), [], settings=settings) is True
    [overall] = pr_ready.named(CHECK)
    assert overall["head_sha"] == HEAD_SHA
    assert (overall["status"], overall["conclusion"]) == ("completed", "success")
    assert overall["output"]["title"] == "No runs needed"
    assert len(pr_ready.check_runs) == 1
    assert 7 not in pr_ready.comments


def test_a_push_with_no_runs_needed_passes_the_aggregate(push_ready, settings):
    """A push that runs nothing concludes the aggregate on the pushed commit."""
    assert reporting.report_upload(push_upload(), [], settings=settings) is True
    [overall] = push_ready.named(CHECK)
    assert (overall["head_sha"], overall["conclusion"]) == (PUSH_SHA, "success")


def test_a_skipped_workspace_fails_the_aggregate_by_name(pr_ready, settings):
    """The aggregate, the workspace's own check and the comment all name the reason."""
    assert reporting.report_upload(pr_upload(), [SKIP], settings=settings) is True
    [overall] = pr_ready.named(CHECK)
    assert (overall["status"], overall["conclusion"]) == ("completed", "failure")
    assert "| roleless | Not run | The workspace has no run role. |" in overall["output"]["summary"]
    [own] = pr_ready.named(f"{CHECK}/roleless")
    assert (own["conclusion"], own["output"]["summary"]) == ("failure", SKIP["reason"])
    [comment] = pr_ready.comments[7]
    assert "roleless" in comment["body"]


def test_a_reported_upload_updates_the_same_aggregate(pr_ready, settings):
    """A redelivered upload lands on the aggregate already there."""
    reporting.report_upload(pr_upload(), [], settings=settings)
    reporting.report_upload(pr_upload(), [], settings=settings)
    assert len(pr_ready.named(CHECK)) == 1


def test_an_upload_on_a_forged_head_posts_nothing(pr_ready, settings):
    """The upload's head is verified the same way a run's is."""
    assert reporting.report_upload(pr_upload(head_sha=OTHER_SHA), [], settings=settings) is False
    assert pr_ready.writes() == []


def test_an_upload_report_swallows_github_failures(pr_ready, settings):
    """A GitHub error never reaches the ingest consumer."""
    pr_ready.failure = 500
    assert reporting.report_upload(pr_upload(), [], settings=settings) is False


def test_an_upload_report_without_an_app_posts_nothing(settings, monkeypatch):
    """No App credentials, no calls."""
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY", raising=False)
    loader.invalidate()
    calls: list[Any] = []
    monkeypatch.setattr(reporting, "http_client", lambda: calls.append(1))
    assert reporting.report_upload(pr_upload(), [], settings=settings) is False
    assert calls == []
    loader.invalidate()


def test_a_run_carrying_a_skip_keeps_the_aggregate_failing(pr_ready, settings):
    """A sibling that could not run fails the aggregate even once the run itself planned."""
    store_run(
        settings,
        "run-1",
        "ws-1",
        "planned_and_finished",
        source="vcs_pr",
        changes={"add": 0, "change": 0, "destroy": 0},
    )
    set_status(
        settings,
        "run-1",
        "planned_and_finished",
        vcs=(repositories.runs(settings).get({"run_id": "run-1"}) or {})["vcs"] | {"skipped": [SKIP]},
    )
    assert reporting.report_run("run-1", settings=settings) is True
    [own] = pr_ready.named("webbpulse-terraform/network")
    assert own["conclusion"] == "success"
    [overall] = pr_ready.named(CHECK)
    assert overall["conclusion"] == "failure"
    assert "| roleless | Not run |" in overall["output"]["summary"]
    assert "roleless" in pr_ready.comments[7][0]["body"]


def test_a_planned_pull_request_run_passes_the_aggregate(pr_ready, settings):
    """A pull request plan with changes is a success: only a failure blocks the merge."""
    store_run(
        settings,
        "run-1",
        "ws-1",
        "planned_and_finished",
        source="vcs_pr",
        changes={"add": 2, "change": 1, "destroy": 1},
    )
    reporting.report_run("run-1", settings=settings)
    [overall] = pr_ready.named(CHECK)
    assert (overall["status"], overall["conclusion"]) == ("completed", "success")
