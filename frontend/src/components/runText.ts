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

/** The page a run opens on. */
export function runPath(run: Pick<Run, 'run_id' | 'workspace_id'>): string {
  return `/workspaces/${run.workspace_id}/runs/${run.run_id}`;
}

/** The run's title: its message, or its id when it has none. */
export function runTitle(run: Run): string {
  return run.message === undefined || run.message === ''
    ? `Run ${shortRunId(run.run_id)}`
    : run.message;
}
