"""Reporting VCS runs back to GitHub through the environment's App.

Every VCS run gets a check run named `webbpulse-terraform/<workspace>` on its commit,
created when the run first appears and updated on each status change. One aggregate
check, `webbpulse-terraform`, sums up every bound workspace's run for that commit, and
a pull request gets one comment, found again by a hidden marker and edited in place.

Nothing is stored for any of it. The check runs are found through GitHub by name and
by `external_id`, which is the run id, and the comment by its marker, so a report can
be retried or replayed from any point and lands on the same objects. The one thing
reporting writes back is what the upload does not carry and the run pages show: the
commit's message and, for a push, the pull request it was merged from, both read
through the App on the first report.

Every ingested push and pull request gets the aggregate, so it can be a required
check. An upload no bound workspace runs on concludes it `success` as "No runs
needed". A workspace the upload matched but could not run, for a missing or
malformed run role, concludes it `failure` naming the workspace and the reason,
and every run started from that upload carries the skip in `vcs.skipped` so later
reports keep the failure.

A held run's check completes as `action_required`. GitHub never reopens a completed
check run, so once the run is confirmed a new check run of the same name carries the
apply, and the held one keeps its conclusion with a summary pointing at its successor.
The aggregate follows the same way. The newest check of a name is the one GitHub
shows and the one every later report finds.

The commit a report lands on is checked through the App first. A push reports on the
delivery's commit once the branch is confirmed to contain it. A pull request
reports on its head, which is recorded rather than trusted, so the head must be a parent of
the signed merge commit and must be, or have been, a commit of that pull request. A
commit that fails either check gets nothing.

Every read and write goes through `webbpulse.integrations.github.GitHubAppClient`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
from boto3.dynamodb.conditions import Key
from webbpulse.integrations.github import (
    CheckRunConclusion,
    CheckRunOutput,
    CheckRunStatus,
    GitHubAppClient,
    GitHubError,
    GitHubNotConfigured,
)

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.db.tables import RUNS_BY_WORKSPACE_INDEX
from ...common.github.loader import github_app_settings
from ...common.workspaces import reads as workspace_reads
from ...common.workspaces import vcs as workspace_vcs
from . import service
from .vcs import PULL_REQUEST_EVENT

_log = logging.getLogger(__name__)

CHECK_NAME: Final = "webbpulse-terraform"
"""The aggregate check's name, and the prefix of every workspace's check."""

COMMENT_MARKER: Final = "<!-- webbpulse-terraform:pr-comment -->"
"""The hidden line the pull request comment is found by."""

SOURCE_PUSH: Final = "vcs_push"
SOURCE_PR: Final = "vcs_pr"

READ_TIMEOUT_SECONDS: Final = 10.0
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 5
RUN_SCAN_LIMIT: Final = 50
"""How many of a workspace's newest runs are looked through for the one on a commit."""

ACTIVE: Final = frozenset({"planning", "planned", "applying"})
TITLES: Final = {
    "pending": "Run queued",
    "planning": "Planning",
    "planned": "Planned",
    "awaiting_confirmation": "Run pending confirmation",
    "applying": "Applying",
    "applied": "Applied",
    "planned_and_finished": "Planned and finished",
    "errored": "Run errored",
    "cancelled": "Run cancelled",
    "discarded": "Run discarded",
}
AUTO_APPLY_TITLE: Final = "Planned, auto-applying"
"""What a run on an auto-apply workspace shows between its plan and its apply, which
is no wait for a person and so is not held as `action_required`."""
CONCLUSIONS: Final[dict[str, CheckRunConclusion]] = {
    "applied": "success",
    "planned_and_finished": "success",
    "errored": "failure",
    "cancelled": "cancelled",
    "discarded": "neutral",
}


class UnverifiedCommit(Exception):
    """GitHub does not confirm the commit a run would report on."""


@dataclass(frozen=True, slots=True)
class CheckState:
    """What one run's check run shows."""

    status: CheckRunStatus
    conclusion: CheckRunConclusion | None
    title: str


def http_client() -> httpx.Client:
    """The HTTP client GitHub calls go through. The seam the tests replace."""
    return httpx.Client(timeout=READ_TIMEOUT_SECONDS, follow_redirects=False)


def check_state(run: Mapping[str, Any]) -> CheckState:
    """The check run status, conclusion and title for a run's current status.

    A push run held for confirmation completes as `action_required`: GitHub offers no
    waiting state a check can rest in indefinitely, and that conclusion is what makes
    the commit show a person is needed rather than a pass or a failure. A run that
    will auto-apply stays in progress instead, since nobody is needed.
    """
    status = str(run.get("status", ""))
    title = TITLES.get(status, status or "Run")
    if status == "pending":
        return CheckState("queued", None, title)
    if status in ACTIVE:
        return CheckState("in_progress", None, title)
    if status == "awaiting_confirmation" and bool(run.get("auto_apply", False)):
        return CheckState("in_progress", None, AUTO_APPLY_TITLE)
    if status == "awaiting_confirmation":
        return CheckState("completed", "action_required", title)
    conclusion = CONCLUSIONS.get(status)
    if conclusion is None:
        return CheckState("in_progress", None, title)
    return CheckState("completed", conclusion, title)


def _counts(values: Any) -> tuple[int, int, int] | None:
    """The add, change and destroy counts one changes object holds."""
    if not isinstance(values, Mapping):
        return None
    return int(values.get("add", 0)), int(values.get("change", 0)), int(values.get("destroy", 0))


def counts_line(run: Mapping[str, Any]) -> str:
    """The run's resource counts the way HCP Terraform words them."""
    applied = _counts(run.get("apply_changes"))
    if str(run.get("status")) == "applied" and applied is not None:
        return f"Apply: {applied[0]} added, {applied[1]} changed, {applied[2]} destroyed."
    planned = _counts(run.get("changes"))
    if planned is None:
        return "Plan not finished yet."
    if planned == (0, 0, 0):
        return "Terraform plan: no changes."
    return f"Terraform plan: {planned[0]} to add, {planned[1]} to change, {planned[2]} to destroy."


def run_url(settings: Settings, run: Mapping[str, Any]) -> str | None:
    """The SPA page for the run, or `None` while the environment has no frontend origin."""
    base = settings.IDENTITY_FRONTEND_BASE_URL.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/workspaces/{run['workspace_id']}/runs/{run['run_id']}"


class GitHubReader:
    """Reads and writes against one repository as the App's installation on it."""

    def __init__(self, client: GitHubAppClient, repository: str, app_id: str) -> None:
        """Hold the App client, the repository and the App's id."""
        self.client = client
        self.repository = repository
        self.app_id = app_id
        self._installation: int | None = None

    @property
    def installation(self) -> int:
        """The installation covering the repository, looked up once."""
        if self._installation is None:
            self._installation = self.client.repository_installation(self.repository)
        return self._installation


def verified_commit(reader: GitHubReader, run: Mapping[str, Any]) -> str:
    """The commit this run reports on, once GitHub confirms it belongs to the run.

    Raises:
        UnverifiedCommit: The commit is not the pushed branch's, or not the pull
            request's head.
    """
    vcs = run.get("vcs") or {}
    sha = str(vcs.get("sha", ""))
    if not sha:
        raise UnverifiedCommit("the run carries no commit")
    if str(run.get("source")) == SOURCE_PUSH:
        branch = str(vcs.get("branch") or "")
        if not branch:
            raise UnverifiedCommit("the push run carries no branch")
        compared = reader.client.compare_commits(reader.repository, branch, sha, installation_id=reader.installation)
        if compared.status not in ("identical", "behind"):
            raise UnverifiedCommit("the commit is not on the pushed branch")
        return sha
    number = vcs.get("pr_number")
    if number is None:
        raise UnverifiedCommit("the pull request run carries no number")
    parents = list(reader.client.get_commit(reader.repository, sha, installation_id=reader.installation).parents)
    if len(parents) < 2:
        raise UnverifiedCommit("the signed commit is not a merge commit")
    head = str(vcs.get("head_sha") or parents[-1])
    if head not in parents[1:]:
        raise UnverifiedCommit("the head is not a parent of the signed merge commit")
    pull = reader.client.get_pull_request(reader.repository, int(number), installation_id=reader.installation)
    if pull.head_sha == head:
        return head
    commits = reader.client.list_pull_request_commits(
        reader.repository, int(number), max_pages=MAX_PAGES, installation_id=reader.installation
    )
    if any(commit.sha == head for commit in commits):
        return head
    raise UnverifiedCommit("the head is not a commit of the pull request")


def find_check_run(reader: GitHubReader, sha: str, name: str, external_id: str | None) -> tuple[int, str] | None:
    """This App's newest check run named `name` on `sha`, matching `external_id` when given.

    Answers its id and status.
    """
    listed = reader.client.list_check_runs(
        reader.repository,
        sha,
        check_name=name,
        app_id=reader.app_id,
        latest=False,
        max_pages=MAX_PAGES,
        installation_id=reader.installation,
    )
    found = [
        (item.id, item.status)
        for item in listed
        if str(item.app_id or "") == reader.app_id
        and item.name == name
        and (external_id is None or item.external_id == external_id)
    ]
    return max(found) if found else None


SUPERSEDED: Final = CheckRunOutput(
    title="Continued in a newer check",
    summary=(
        "Progress moved on after this check completed, and GitHub does not reopen a completed check, "
        "so it continues on a newer check run of the same name."
    ),
)
"""What a completed check says once a newer one of its name takes over."""


def retire_check_run(reader: GitHubReader, check_run_id: int) -> None:
    """Point a completed check run at its successor, leaving its conclusion alone.

    Best effort: the successor is what matters, so a refusal here is dropped.
    """
    try:
        reader.client.update_check_run(
            reader.repository, check_run_id, output=SUPERSEDED, installation_id=reader.installation
        )
    except GitHubError:
        return


def upsert_check_run(
    reader: GitHubReader,
    sha: str,
    name: str,
    state: CheckState,
    output: CheckRunOutput,
    *,
    details_url: str | None,
    external_id: str | None,
) -> None:
    """Create the named check run on `sha`, or update the one already there.

    A completed check that has to show progress again, a held run that was
    confirmed, is never patched back open: GitHub keeps a completed check
    completed. A new one of the same name is created instead and the old one is
    retired, and the newest of a name is always the one found.
    """
    existing = find_check_run(reader, sha, name, external_id)
    fields: dict[str, Any] = {
        "conclusion": state.conclusion,
        "output": output,
        "details_url": details_url,
        "external_id": external_id,
        "installation_id": reader.installation,
    }
    if existing is not None and existing[1] == "completed" and state.status != "completed":
        _log.info(
            "A completed check run was followed by a new one of the same name.",
            extra={"event": "runs.report.check_replaced", "check_run_id": existing[0], "check_name": name},
        )
        reader.client.create_check_run(reader.repository, name=name, head_sha=sha, status=state.status, **fields)
        retire_check_run(reader, existing[0])
    elif existing is None:
        reader.client.create_check_run(reader.repository, name=name, head_sha=sha, status=state.status, **fields)
    else:
        reader.client.update_check_run(reader.repository, existing[0], status=state.status, **fields)


def _newest_matching(workspace_id: str, match: Any, *, settings: Settings) -> dict[str, Any] | None:
    """The workspace's newest run that `match` accepts, among its newest few."""
    table = repositories.runs(settings)
    for index, item in enumerate(
        table.iter_query(Key("workspace_id").eq(workspace_id), index_name=RUNS_BY_WORKSPACE_INDEX, ascending=False)
    ):
        if index >= RUN_SCAN_LIMIT:
            return None
        if item.get("run_id") and match(item):
            return dict(item)
    return None


def sibling_runs(run: Mapping[str, Any], *, by_pull_request: bool, settings: Settings) -> list[dict[str, Any]]:
    """The newest run per bound workspace from the same commit, or the same pull request.

    Each comes back with the workspace's `name` added as `workspace_name`.
    """
    vcs = run.get("vcs") or {}
    repository_id = str(vcs.get("repository_id", ""))

    def match(item: Mapping[str, Any]) -> bool:
        """Whether a run came from the same commit, or the same pull request."""
        other = item.get("vcs") or {}
        if str(item.get("source")) != str(run.get("source")) or str(other.get("repository_id", "")) != repository_id:
            return False
        if by_pull_request:
            return other.get("pr_number") is not None and int(other["pr_number"]) == int(vcs["pr_number"])
        return str(other.get("sha", "")) == str(vcs.get("sha", ""))

    found = []
    for workspace in workspace_vcs.bound_workspaces(str(vcs.get("repo", "")), repository_id, settings=settings):
        newest = _newest_matching(str(workspace["workspace_id"]), match, settings=settings)
        if newest is not None:
            newest["workspace_name"] = str(workspace.get("name", workspace["workspace_id"]))
            found.append(newest)
    return sorted(found, key=lambda item: item["workspace_name"])


def with_current(siblings: list[dict[str, Any]], run: Mapping[str, Any], workspace_name: str) -> list[dict[str, Any]]:
    """The sibling runs with this run's current state in its workspace's place.

    The siblings are read off a secondary index, which trails the table, so the run
    being reported can come back a status behind or be missing altogether. The run
    in hand is the newest word on itself, so it replaces its workspace's entry
    unless that entry is a newer run.
    """
    workspace_id = str(run["workspace_id"])
    run_id = str(run["run_id"])
    kept = [
        item
        for item in siblings
        if str(item.get("workspace_id")) != workspace_id or str(item.get("run_id", "")) > run_id
    ]
    if any(str(item.get("workspace_id")) == workspace_id for item in kept):
        return kept
    kept.append(dict(run) | {"workspace_name": workspace_name})
    return sorted(kept, key=lambda item: item["workspace_name"])


def skipped_workspaces(runs: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The workspaces the runs' uploads matched but could not run, by name, once each."""
    found: dict[str, str] = {}
    for run in runs:
        for item in (run.get("vcs") or {}).get("skipped") or []:
            if isinstance(item, Mapping) and item.get("workspace_name"):
                found[str(item["workspace_name"])] = str(item.get("reason", ""))
    return [{"workspace_name": name, "reason": reason} for name, reason in sorted(found.items())]


def aggregate_state(runs: Iterable[Mapping[str, Any]], skipped: Iterable[Mapping[str, Any]] = ()) -> CheckState:
    """One check state for a set of runs and skipped workspaces.

    A skipped workspace fails it outright. Otherwise it is still going, failed,
    held or finished, and no runs at all is a success.
    """
    if list(skipped):
        return CheckState("completed", "failure", "Workspace could not run")
    states = [check_state(run) for run in runs]
    if not states:
        return CheckState("completed", "success", "No runs needed")
    if any(state.status != "completed" for state in states):
        return CheckState("in_progress", None, "Runs in progress")
    conclusions = {state.conclusion for state in states}
    if "failure" in conclusions:
        return CheckState("completed", "failure", "Runs errored")
    if "action_required" in conclusions:
        return CheckState("completed", "action_required", "Runs pending confirmation")
    if conclusions == {"success"}:
        return CheckState("completed", "success", "All runs finished")
    return CheckState("completed", "neutral", "Runs finished")


def _row(run: Mapping[str, Any], settings: Settings) -> str:
    """One Markdown table row for a run."""
    title = TITLES.get(str(run.get("status")), str(run.get("status")))
    url = run_url(settings, run)
    shown = f"[{title}]({url})" if url else title
    return f"| {run['workspace_name']} | {shown} | {counts_line(run)} |"


def runs_table(runs: Iterable[Mapping[str, Any]], settings: Settings, skipped: Iterable[Mapping[str, Any]] = ()) -> str:
    """The workspaces, their statuses and counts, then the skipped ones, as a Markdown table."""
    lines = ["| Workspace | Status | Changes |", "| --- | --- | --- |"]
    lines.extend(_row(run, settings) for run in runs)
    lines.extend(f"| {item['workspace_name']} | Not run | {item['reason']} |" for item in skipped)
    return "\n".join(lines)


def comment_body(
    runs: list[dict[str, Any]], head: str, settings: Settings, skipped: Iterable[Mapping[str, Any]] = ()
) -> str:
    """The pull request comment: the marker, a heading and the table."""
    return "\n".join(
        [COMMENT_MARKER, f"### WebbPulse Terraform runs for {head[:7]}", "", runs_table(runs, settings, skipped), ""]
    )


def upsert_comment(reader: GitHubReader, number: int, body: str) -> None:
    """Edit this App's marked comment on the pull request, or post it."""
    comments = reader.client.list_issue_comments(
        reader.repository, number, max_pages=MAX_PAGES, installation_id=reader.installation
    )
    marked = [item for item in comments if COMMENT_MARKER in item.body and item.user_type == "Bot"]
    if marked:
        oldest = min(marked, key=lambda item: item.id)
        if oldest.body != body:
            reader.client.update_issue_comment(reader.repository, oldest.id, body, installation_id=reader.installation)
    else:
        reader.client.create_issue_comment(reader.repository, number, body, installation_id=reader.installation)


def record_message(reader: GitHubReader, run: Mapping[str, Any], sha: str, *, settings: Settings) -> None:
    """Read the reported commit's message and keep it on the run, once.

    Best effort: a commit GitHub will not answer for leaves the run without a
    message rather than stopping the report.
    """
    if (run.get("vcs") or {}).get("commit_message"):
        return
    try:
        commit = reader.client.get_commit(reader.repository, sha, installation_id=reader.installation)
    except (GitHubError, ValueError):
        return
    message = commit.message.strip()
    if message:
        service.record_commit_message(str(run["run_id"]), message, settings=settings)


def record_pull_request(reader: GitHubReader, run: Mapping[str, Any], sha: str, *, settings: Settings) -> None:
    """Find the pull request a push run's commit was merged from and keep it on the run, once.

    The merged pull request whose merge commit is the pushed commit wins, then any
    merged one, then the first listed. Best effort: a commit GitHub will not answer
    for, or one no pull request produced, leaves the run without a link.
    """
    if str(run.get("source")) != SOURCE_PUSH or (run.get("vcs") or {}).get("pull_request"):
        return
    try:
        listed = reader.client.list_commit_pull_requests(
            reader.repository, sha, max_pages=1, installation_id=reader.installation
        )
    except (GitHubError, ValueError):
        return
    pulls = [item for item in listed if item.number]
    if not pulls:
        return
    chosen = next(
        (item for item in pulls if item.merge_commit_sha == sha and item.merged_at),
        next((item for item in pulls if item.merged_at), pulls[0]),
    )
    number = chosen.number
    url = chosen.html_url or f"https://github.com/{reader.repository}/pull/{number}"
    service.record_pull_request(str(run["run_id"]), number, url, settings=settings)


def publish(reader: GitHubReader, run: Mapping[str, Any], *, settings: Settings) -> str:
    """Report one run: its own check, the commit's aggregate and the pull request comment.

    Returns the commit the reports landed on.
    """
    sha = verified_commit(reader, run)
    record_message(reader, run, sha, settings=settings)
    record_pull_request(reader, run, sha, settings=settings)
    workspace = workspace_reads.get_workspace(str(run["workspace_id"]), settings=settings)
    workspace_name = str(workspace.get("name", run["workspace_id"]))
    name = f"{CHECK_NAME}/{workspace_name}"
    state = check_state(run)
    url = run_url(settings, run)
    summary = counts_line(run)
    if run.get("error"):
        summary = f"{summary}\n\n{run['error']}"
    upsert_check_run(
        reader,
        sha,
        name,
        state,
        CheckRunOutput(title=state.title, summary=summary, text=str(run.get("message") or "") or None),
        details_url=url,
        external_id=str(run["run_id"]),
    )

    siblings = with_current(sibling_runs(run, by_pull_request=False, settings=settings), run, workspace_name)
    skipped = skipped_workspaces(siblings)
    overall = aggregate_state(siblings, skipped)
    upsert_check_run(
        reader,
        sha,
        CHECK_NAME,
        overall,
        CheckRunOutput(title=overall.title, summary=runs_table(siblings, settings, skipped)),
        details_url=None,
        external_id=None,
    )

    if str(run.get("source")) == SOURCE_PR:
        number = int((run.get("vcs") or {})["pr_number"])
        latest = with_current(sibling_runs(run, by_pull_request=True, settings=settings), run, workspace_name)
        upsert_comment(reader, number, comment_body(latest, sha, settings, skipped_workspaces(latest)))
    return sha


NO_RUNS_SUMMARY: Final = "No workspace bound to this repository runs on this change."
SKIPPED_TITLE: Final = "Run not started"


def upload_as_run(upload: Mapping[str, Any]) -> dict[str, Any]:
    """The run shaped view of an upload that `verified_commit` reads."""
    is_pr = str(upload.get("event")) == PULL_REQUEST_EVENT
    return {
        "source": SOURCE_PR if is_pr else SOURCE_PUSH,
        "vcs": {
            "repo": upload.get("repo"),
            "repository_id": upload.get("repository_id"),
            "sha": upload.get("sha"),
            "branch": upload.get("branch"),
            "pr_number": upload.get("pr_number"),
            "head_sha": upload.get("head_sha"),
        },
    }


def publish_upload(
    reader: GitHubReader, upload: Mapping[str, Any], skipped: list[dict[str, str]], *, settings: Settings
) -> str:
    """Report an upload that started no run: a failing check per skipped workspace and the aggregate.

    Returns the commit the reports landed on.
    """
    run = upload_as_run(upload)
    sha = verified_commit(reader, run)
    for item in skipped:
        upsert_check_run(
            reader,
            sha,
            f"{CHECK_NAME}/{item['workspace_name']}",
            CheckState("completed", "failure", SKIPPED_TITLE),
            CheckRunOutput(title=SKIPPED_TITLE, summary=item["reason"]),
            details_url=None,
            external_id=None,
        )
    overall = aggregate_state([], skipped)
    summary = runs_table([], settings, skipped) if skipped else NO_RUNS_SUMMARY
    upsert_check_run(
        reader,
        sha,
        CHECK_NAME,
        overall,
        CheckRunOutput(title=overall.title, summary=summary),
        details_url=None,
        external_id=None,
    )
    if skipped and run["source"] == SOURCE_PR:
        upsert_comment(reader, int(run["vcs"]["pr_number"]), comment_body([], sha, settings, skipped))
    return sha


@contextmanager
def app_reader(repository: str, settings: Settings) -> Iterator[GitHubReader | None]:
    """A reader on `repository` through the environment's App, or `None` when there is no App."""
    try:
        app = github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    except GitHubNotConfigured:
        yield None
        return
    with http_client() as http:
        client = GitHubAppClient.from_settings(app, client=http)
        yield GitHubReader(client, repository, str(app.app_id))


def report_upload(
    upload: Mapping[str, Any], skipped: list[dict[str, str]], *, settings: Settings | None = None
) -> bool:
    """Report an upload that started no run to GitHub. Never raises; returns whether anything was posted."""
    resolved = settings or get_settings()
    extra: dict[str, Any] = {"upload_id": upload.get("upload_id"), "repository": upload.get("repo")}
    try:
        with app_reader(str(upload["repo"]), resolved) as reader:
            if reader is None:
                _log.info("No GitHub App to report an upload through.", extra={"event": "runs.report.no_app", **extra})
                return False
            sha = publish_upload(reader, upload, skipped, settings=resolved)
        _log.info(
            "Reported an upload that started no run.",
            extra={"event": "runs.report.upload_sent", "sha": sha, "skipped": len(skipped), **extra},
        )
        return True
    except UnverifiedCommit as exc:
        _log.warning(
            "Skipped reporting an upload on an unverified commit.",
            extra={"event": "runs.report.unverified", "reason": str(exc), **extra},
        )
    except Exception as exc:
        _log.warning(
            "Reporting an upload to GitHub failed.",
            extra={"event": "runs.report.failed", "error_type": type(exc).__name__, **extra},
        )
    return False


def report_run(run_id: str, *, image: Mapping[str, Any] | None = None, settings: Settings | None = None) -> bool:
    """Report one VCS run to GitHub. Never raises; returns whether anything was posted.

    `image` is the run as a stream record wrote it. Reporting that rather than a
    fresh read is what lets every transition show: a run inserted `pending` and
    started a moment later would otherwise be read as `planning` twice, and its
    check would never show queued. A stream delivers one item's records in order,
    so the image is never older than one already reported.

    A reporting fault is logged and dropped, so a GitHub outage or a missing App can
    never hold up, retry or fail a run.
    """
    resolved = settings or get_settings()
    extra: dict[str, Any] = {"run_id": run_id}
    try:
        run = dict(image) if image is not None else service.get_run(run_id, settings=resolved)
        vcs = run.get("vcs") or {}
        if str(run.get("source")) not in (SOURCE_PUSH, SOURCE_PR) or not vcs.get("repo"):
            return False
        extra |= {"repository": vcs["repo"], "status": run.get("status")}
        with app_reader(str(vcs["repo"]), resolved) as reader:
            if reader is None:
                _log.info("No GitHub App to report a run through.", extra={"event": "runs.report.no_app", **extra})
                return False
            sha = publish(reader, run, settings=resolved)
        _log.info("Reported a run to GitHub.", extra={"event": "runs.report.sent", "sha": sha, **extra})
        return True
    except UnverifiedCommit as exc:
        _log.warning(
            "Skipped reporting a run on an unverified commit.",
            extra={"event": "runs.report.unverified", "reason": str(exc), **extra},
        )
    except Exception as exc:
        _log.warning(
            "Reporting a run to GitHub failed.",
            extra={"event": "runs.report.failed", "error_type": type(exc).__name__, **extra},
        )
    return False


__all__ = [
    "CHECK_NAME",
    "COMMENT_MARKER",
    "CheckState",
    "GitHubReader",
    "UnverifiedCommit",
    "aggregate_state",
    "check_state",
    "counts_line",
    "http_client",
    "app_reader",
    "publish",
    "publish_upload",
    "record_message",
    "record_pull_request",
    "retire_check_run",
    "report_run",
    "report_upload",
    "run_url",
    "sibling_runs",
    "skipped_workspaces",
    "upload_as_run",
    "verified_commit",
    "with_current",
]
