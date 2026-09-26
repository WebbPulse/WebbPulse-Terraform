"""Reporting VCS runs to GitHub: check runs, the aggregate check and the PR comment.

GitHub is replaced at the HTTP layer with `httpx.MockTransport`, so the real App
client signs a real App JWT, exchanges it for an installation token and sends the
real check run and comment calls. Runs and workspaces are written straight into
moto's tables, since what is under test is what a stored run reports, not how it
got there.
"""

from __future__ import annotations

import json
import re
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


@dataclass
class FakeGitHub:
    """The calls reporting makes, answered from plain dictionaries."""

    parents: dict[str, list[str]] = field(default_factory=dict)
    pull_heads: dict[int, str] = field(default_factory=dict)
    pull_commits: dict[int, list[str]] = field(default_factory=dict)
    compare: dict[str, str] = field(default_factory=dict)
    check_runs: list[dict[str, Any]] = field(default_factory=list)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    failure: int | None = None

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
        if match := re.fullmatch(rf"{base}/commits/(\w+)", path):
            parents = self.parents.get(match.group(1))
            if parents is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"sha": match.group(1), "parents": [{"sha": sha} for sha in parents]})
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
            found.update(json.loads(request.content))
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


def test_a_held_push_run_asks_for_action_then_a_new_check_follows_confirmation(push_ready, settings):
    """`action_required` while held; once applying, a fresh check shows progress."""
    store_run(settings, "run-1", "ws-1", "awaiting_confirmation", changes={"add": 3, "change": 0, "destroy": 1})
    reporting.report_run("run-1", settings=settings)
    [held] = push_ready.named("webbpulse-terraform/network")
    assert (held["status"], held["conclusion"]) == ("completed", "action_required")
    assert held["output"]["title"] == "Run pending confirmation"
    assert held["output"]["summary"] == "Terraform plan: 3 to add, 0 to change, 1 to destroy."

    set_status(settings, "run-1", "applying")
    reporting.report_run("run-1", settings=settings)
    held_again, applying = push_ready.named("webbpulse-terraform/network")
    assert held_again["conclusion"] == "action_required"
    assert applying["status"] == "in_progress"
    assert applying["external_id"] == "run-1"


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
