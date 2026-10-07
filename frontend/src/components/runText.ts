/** Words and links for a run, shared by the lists and the run page. */

import type { Run } from '../api';
import { shortRunId } from './format';

/** The plan counts as a single line, or a hyphen when the plan has not reported. */
export function changeSummary(run: Run): string {
  if (run.changes === null || run.changes === undefined) {
    return '-';
  }
  const { add, change, destroy } = run.changes;
  return `+${String(add)} ~${String(change)} -${String(destroy)}`;
}

/** Whether a newer commit of the same pull request replaced this plan. */
export function isSuperseded(run: Pick<Run, 'superseded_by'>): boolean {
  return run.superseded_by !== undefined && run.superseded_by !== null;
}

/** The page a run opens on. */
export function runPath(run: Pick<Run, 'run_id' | 'workspace_id'>): string {
  return `/workspaces/${run.workspace_id}/runs/${run.run_id}`;
}

/**
 * The run's title. A VCS run is titled by its commit's first line, as HCP
 * Terraform's run list does; any other run by its message, or its id.
 */
export function runTitle(run: Run): string {
  const headline = commitHeadline(run);
  if (headline !== null) {
    return headline;
  }
  if (run.message !== undefined && run.message !== '') {
    return run.message;
  }
  return `Run ${shortRunId(run.run_id)}`;
}

/** The first line of the commit message a VCS run was started from, if known. */
export function commitHeadline(run: Pick<Run, 'vcs'>): string | null {
  const message = run.vcs?.commit_message;
  if (message === undefined || message === null) {
    return null;
  }
  const first = message.split('\n', 1)[0]?.trim() ?? '';
  return first === '' ? null : first;
}

/** Where a VCS run came from on GitHub: its trigger, its commit and its pull request. */
export interface RunSource {
  /** The sentence HCP Terraform shows, such as "Triggered via GitHub from pull request #91". */
  trigger: string;
  sha: string;
  shortSha: string;
  commitUrl: string;
  /** The pull request a pull request run plans, or the one a push was merged from. */
  prNumber: number | null;
  prUrl: string | null;
}

/** The GitHub source of a VCS run, or null for a run started by hand or by the API. */
export function runSource(run: Pick<Run, 'vcs'>): RunSource | null {
  const vcs = run.vcs;
  if (vcs === undefined || vcs === null) {
    return null;
  }
  const repoUrl = `https://github.com/${vcs.repo}`;
  const prNumber = vcs.pr_number ?? null;
  const merged = vcs.pull_request ?? null;
  const sha =
    prNumber !== null && vcs.head_sha !== undefined && vcs.head_sha !== null
      ? vcs.head_sha
      : vcs.sha;
  const branch = vcs.branch ?? vcs.ref.replace(/^refs\/(heads|tags)\//, '');
  return {
    trigger:
      prNumber === null
        ? `Triggered via GitHub from a push to ${branch}`
        : `Triggered via GitHub from pull request #${String(prNumber)}`,
    sha,
    shortSha: sha.slice(0, 7),
    commitUrl: `${repoUrl}/commit/${sha}`,
    prNumber: prNumber ?? merged?.number ?? null,
    prUrl:
      prNumber !== null
        ? `${repoUrl}/pull/${String(prNumber)}`
        : (merged?.url ?? null),
  };
}

/** Whether the run plans the destruction of every resource the workspace manages. */
export function isDestroyRun(run: Pick<Run, 'is_destroy'>): boolean {
  return run.is_destroy === true;
}

/** What kind of run it is, as a sentence-case phrase such as "Destroy run". */
export function runKind(run: Pick<Run, 'is_destroy' | 'plan_only'>): string {
  if (isDestroyRun(run)) {
    return run.plan_only ? 'Plan only destroy run' : 'Destroy run';
  }
  return run.plan_only ? 'Plan only run' : 'Plan and apply run';
}
