/** The run list, shared by the runs page and a workspace's runs page. */

import { Link } from 'react-router-dom';

import type { Run } from '../api';
import { EmptyState } from './EmptyState';
import { formatDateTime, formatRelative, shortRunId } from './format';
import { changeSummary, runPath, runTitle } from './runText';
import { StateBadge } from './StateBadge';

/** Props for {@link RunList}. */
export interface RunListProps {
  runs: readonly Run[];
  /** Workspace names by id, when the list spans workspaces. */
  workspaceNames?: ReadonlyMap<string, string>;
  /** Words for an empty list. Defaults to the plain sentence. */
  emptyHint?: string;
}

/** A list of runs, or a sentence when there are none. */
export function RunList({
  runs,
  workspaceNames,
  emptyHint = 'Start one from a configuration version on a workspace.',
}: RunListProps): React.ReactElement {
  if (runs.length === 0) {
    return <EmptyState title="No runs yet." hint={emptyHint} />;
  }
  return (
    <ul
      aria-label="Runs"
      className="divide-y divide-line rounded-lg border border-line bg-panel"
    >
      {runs.map((run) => (
        <li
          key={run.run_id}
          className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
        >
          <div className="min-w-0 flex-1">
            <Link
              to={runPath(run)}
              className="block truncate text-sm font-medium text-text-strong hover:text-brand-300"
            >
              {runTitle(run)}
            </Link>
            <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-text-faint">
              <span className="font-mono" title={run.run_id}>
                #{shortRunId(run.run_id)}
              </span>
              <span aria-hidden="true">|</span>
              <span>
                {run.plan_only ? 'plan only run' : 'plan and apply run'}
              </span>
              {workspaceNames === undefined ? null : (
                <>
                  <span aria-hidden="true">|</span>
                  <Link
                    to={`/workspaces/${run.workspace_id}`}
                    className="hover:text-text-strong"
                  >
                    {workspaceNames.get(run.workspace_id) ?? run.workspace_id}
                  </Link>
                </>
              )}
              <span aria-hidden="true">|</span>
              <span className="font-mono">{changeSummary(run)}</span>
            </p>
          </div>
          <div className="flex items-center gap-3">
            <StateBadge state={run.status} />
            <span
              className="text-xs whitespace-nowrap text-text-faint"
              title={formatDateTime(run.created_at)}
            >
              {formatRelative(run.created_at)}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
