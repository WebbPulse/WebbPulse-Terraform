/** The run list, shared by the runs page and a workspace's runs page. */

import { Link } from 'react-router-dom';

import { runGroup, type Run } from '../api';
import { EmptyState } from './EmptyState';
import { formatDateTime, formatRelative, shortRunId } from './format';
import { DestroyBadge } from './DestroyBadge';
import {
  changeSummary,
  isDestroyRun,
  runKind,
  runPath,
  runTitle,
} from './runText';
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
      {runs.map((run) => {
        const needsAction = runGroup(run) === 'attention';
        return (
          <li
            key={run.run_id}
            data-run-id={run.run_id}
            data-needs-action={needsAction}
            className={`flex flex-wrap items-center gap-x-4 gap-y-2 border-l-2 px-4 py-3 ${
              needsAction
                ? 'border-l-warning bg-warning-soft/30'
                : 'border-l-transparent'
            }`}
          >
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-2">
                <Link
                  to={runPath(run)}
                  className="block truncate text-sm font-medium text-text-strong hover:text-accent hover:underline"
                >
                  {runTitle(run)}
                </Link>
                {isDestroyRun(run) ? <DestroyBadge /> : null}
              </div>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-text-faint">
                <span className="font-mono" title={run.run_id}>
                  #{shortRunId(run.run_id)}
                </span>
                <span aria-hidden="true">|</span>
                <span>{runKind(run).toLowerCase()}</span>
                {workspaceNames === undefined ? null : (
                  <>
                    <span aria-hidden="true">|</span>
                    <Link
                      to={`/workspaces/${run.workspace_id}`}
                      className="hover:text-accent hover:underline"
                    >
                      {workspaceNames.get(run.workspace_id) ?? run.workspace_id}
                    </Link>
                  </>
                )}
                <span aria-hidden="true">|</span>
                <ChangeCounts run={run} />
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
        );
      })}
    </ul>
  );
}

/** A run's plan counts, each in its own colour, or a hyphen before a plan. */
function ChangeCounts({ run }: { run: Run }): React.ReactElement {
  if (run.changes === null || run.changes === undefined) {
    return <span className="font-mono">{changeSummary(run)}</span>;
  }
  const { add, change, destroy } = run.changes;
  return (
    <span
      className="font-mono tabular-nums"
      aria-label={`${String(add)} to add, ${String(change)} to change, ${String(destroy)} to destroy`}
    >
      <span className="text-add">+{add}</span>{' '}
      <span className="text-change">~{change}</span>{' '}
      <span className="text-destroy">-{destroy}</span>
    </span>
  );
}
