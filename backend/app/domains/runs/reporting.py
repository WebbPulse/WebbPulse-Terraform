"""Reporting VCS runs back to GitHub through the environment's App.

Every VCS run gets a check run named `webbpulse-terraform/<workspace>` on its commit,
created when the run first appears and updated on each status change. One aggregate
check, `webbpulse-terraform`, sums up every bound workspace's run for that commit, and
a pull request gets one comment, found again by a hidden marker and edited in place.

Nothing is stored for any of it. The check runs are found through GitHub by name and
by `external_id`, which is the run id, and the comment by its marker, so a report can
be retried or replayed from any point and lands on the same objects.

The commit a report lands on is checked through the App first. A push reports on the
token's signed commit once the branch is confirmed to contain it. A pull request
reports on its head, which the workflow sent unsigned, so the head must be a parent of
the signed merge commit and must be, or have been, a commit of that pull request. A
commit that fails either check gets nothing.

`webbpulse.integrations.github.GitHubAppClient` writes but does not read, so the reads
here go through a plain `httpx` call with the client's installation token.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx
from boto3.dynamodb.conditions import Key
from webbpulse.integrations.github import (
    ACCEPT,
    API_ROOT,
    API_VERSION,
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
    the commit show a person is needed rather than a pass or a failure.
    """
    status = str(run.get("status", ""))
    title = TITLES.get(status, status or "Run")
    if status == "pending":
        return CheckState("queued", None, title)
    if status in ACTIVE:
        return CheckState("in_progress", None, title)
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

    def __init__(self, client: GitHubAppClient, http: httpx.Client, repository: str, app_id: str) -> None:
        """Hold the App client, the HTTP client its reads use, and the repository."""
        self.client = client
        self.http = http
        self.repository = repository
        self.app_id = app_id
        self._installation: int | None = None

    @property
    def installation(self) -> int:
        """The installation covering the repository, looked up once."""
        if self._installation is None:
            self._installation = self.client.repository_installation(self.repository)
        return self._installation

    def get(self, suffix: str, params: Mapping[str, Any] | None = None) -> Any:
        """One GET under the repository, answering the parsed body or raising `GitHubError`."""
        token = self.client.installation_token(self.installation)
        path = f"/repos/{self.repository}{suffix}"
        try:
            response = self.http.get(
                f"{API_ROOT}{path}",
                params=dict(params or {}),
                headers={"Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION, "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GET {path} did not answer", method="GET", path=path) from exc
        if response.status_code >= 400:
            raise GitHubError(
                f"GET {path} answered {response.status_code}", method="GET", path=path, status_code=response.status_code
            )
        return response.json()

    def pages(self, suffix: str, params: Mapping[str, Any] | None = None, key: str | None = None) -> list[Any]:
        """Every item a paged listing answers, up to `MAX_PAGES` pages."""
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            body = self.get(suffix, {**(params or {}), "per_page": PAGE_SIZE, "page": page})
            batch = body.get(key, []) if key and isinstance(body, dict) else body
            batch = batch if isinstance(batch, list) else []
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return items


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
        compared = reader.get(f"/compare/{branch}...{sha}")
        if str(compared.get("status")) not in ("identical", "behind"):
            raise UnverifiedCommit("the commit is not on the pushed branch")
        return sha
    number = vcs.get("pr_number")
    if number is None:
        raise UnverifiedCommit("the pull request run carries no number")
    commit = reader.get(f"/commits/{sha}")
    parents = [str(parent.get("sha", "")) for parent in commit.get("parents", []) if isinstance(parent, Mapping)]
    if len(parents) < 2:
        raise UnverifiedCommit("the signed commit is not a merge commit")
    head = str(vcs.get("head_sha") or parents[-1])
    if head not in parents[1:]:
        raise UnverifiedCommit("the head is not a parent of the signed merge commit")
    pull = reader.get(f"/pulls/{int(number)}")
    if str((pull.get("head") or {}).get("sha", "")) == head:
        return head
    commits = reader.pages(f"/pulls/{int(number)}/commits")
    if any(isinstance(item, Mapping) and item.get("sha") == head for item in commits):
        return head
    raise UnverifiedCommit("the head is not a commit of the pull request")


def _own(item: Mapping[str, Any], app_id: str) -> bool:
    """Whether a check run was created by this App."""
    app = item.get("app")
    return isinstance(app, Mapping) and str(app.get("id", "")) == app_id


def find_check_run(reader: GitHubReader, sha: str, name: str, external_id: str | None) -> tuple[int, str] | None:
    """This App's newest check run named `name` on `sha`, matching `external_id` when given.

    Answers its id and status.
    """
    listed = reader.pages(
        f"/commits/{sha}/check-runs",
        {"check_name": name, "filter": "all", "app_id": reader.app_id},
        key="check_runs",
    )
    found = [
        (int(item["id"]), str(item.get("status", "")))
        for item in listed
        if isinstance(item, Mapping)
        and _own(item, reader.app_id)
        and item.get("name") == name
        and (external_id is None or item.get("external_id") == external_id)
    ]
    return max(found) if found else None


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

    A completed check that has to show progress again, a held run that was confirmed,
    is replaced by a new one of the same name rather than reopened, and the newest of
    a name is always the one that is found and shown.
    """
    existing = find_check_run(reader, sha, name, external_id)
    if existing is not None and existing[1] == "completed" and state.status != "completed":
        existing = None
    fields: dict[str, Any] = {
        "conclusion": state.conclusion,
        "output": output,
        "details_url": details_url,
        "external_id": external_id,
        "installation_id": reader.installation,
    }
    if existing is None:
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


def aggregate_state(runs: Iterable[Mapping[str, Any]]) -> CheckState:
    """One check state for a set of runs: still going, failed, held, or finished."""
    states = [check_state(run) for run in runs]
    if not states:
        return CheckState("queued", None, "No runs")
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


def runs_table(runs: Iterable[Mapping[str, Any]], settings: Settings) -> str:
    """The workspaces, their statuses and counts, as a Markdown table."""
    lines = ["| Workspace | Status | Changes |", "| --- | --- | --- |"]
    lines.extend(_row(run, settings) for run in runs)
    return "\n".join(lines)


def comment_body(runs: list[dict[str, Any]], head: str, settings: Settings) -> str:
    """The pull request comment: the marker, a heading and the table."""
    return "\n".join(
        [COMMENT_MARKER, f"### WebbPulse Terraform runs for {head[:7]}", "", runs_table(runs, settings), ""]
    )


def upsert_comment(reader: GitHubReader, number: int, body: str) -> None:
    """Edit this App's marked comment on the pull request, or post it."""
    comments = reader.pages(f"/issues/{number}/comments")
    marked = [
        item
        for item in comments
        if isinstance(item, Mapping)
        and COMMENT_MARKER in str(item.get("body", ""))
        and str((item.get("user") or {}).get("type", "")) == "Bot"
    ]
    if marked:
        oldest = min(marked, key=lambda item: int(item["id"]))
        if oldest.get("body") != body:
            reader.client.update_issue_comment(
                reader.repository, int(oldest["id"]), body, installation_id=reader.installation
            )
    else:
        reader.client.create_issue_comment(reader.repository, number, body, installation_id=reader.installation)


def publish(reader: GitHubReader, run: Mapping[str, Any], *, settings: Settings) -> str:
    """Report one run: its own check, the commit's aggregate and the pull request comment.

    Returns the commit the reports landed on.
    """
    sha = verified_commit(reader, run)
    workspace = workspace_reads.get_workspace(str(run["workspace_id"]), settings=settings)
    name = f"{CHECK_NAME}/{workspace.get('name', run['workspace_id'])}"
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

    siblings = sibling_runs(run, by_pull_request=False, settings=settings)
    overall = aggregate_state(siblings)
    upsert_check_run(
        reader,
        sha,
        CHECK_NAME,
        overall,
        CheckRunOutput(title=overall.title, summary=runs_table(siblings, settings)),
        details_url=None,
        external_id=None,
    )

    if str(run.get("source")) == SOURCE_PR:
        number = int((run.get("vcs") or {})["pr_number"])
        latest = sibling_runs(run, by_pull_request=True, settings=settings)
        upsert_comment(reader, number, comment_body(latest, sha, settings))
    return sha


def report_run(run_id: str, *, settings: Settings | None = None) -> bool:
    """Report one VCS run to GitHub. Never raises; returns whether anything was posted.

    A reporting fault is logged and dropped, so a GitHub outage or a missing App can
    never hold up, retry or fail a run.
    """
    resolved = settings or get_settings()
    extra: dict[str, Any] = {"run_id": run_id}
    try:
        run = service.get_run(run_id, settings=resolved)
        vcs = run.get("vcs") or {}
        if str(run.get("source")) not in (SOURCE_PUSH, SOURCE_PR) or not vcs.get("repo"):
            return False
        extra |= {"repository": vcs["repo"], "status": run.get("status")}
        try:
            app = github_app_settings(resolved.app_secret_arn, region_name=resolved.AWS_REGION_NAME)
        except GitHubNotConfigured:
            _log.info("No GitHub App to report a run through.", extra={"event": "runs.report.no_app", **extra})
            return False
        with http_client() as http:
            client = GitHubAppClient.from_settings(app, client=http)
            reader = GitHubReader(client, http, str(vcs["repo"]), str(app.app_id))
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
    "publish",
    "report_run",
    "run_url",
    "sibling_runs",
    "verified_commit",
]
