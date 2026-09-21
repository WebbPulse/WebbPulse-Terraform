/** The workspace's runs: the current one on top, then the list with its filters. */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { isActive, runGroup, type Run, type RunGroup } from '../../api';
import {
  ErrorNotice,
  RunList,
  Spinner,
  StateBadge,
  Tabs,
  changeSummary,
  formatDateTime,
  formatRelative,
  runPath,
  runTitle,
} from '../../components';
import { useWorkspace } from '../workspaceContext';

/** The filter tabs above the list. */
type Filter = 'all' | RunGroup;

/** The run shown on top: the one moving, or the newest when none is. */
function currentRun(runs: readonly Run[]): Run | null {
  return runs.find((run) => isActive(run.status)) ?? runs[0] ?? null;
}

/** The runs page. */
export function RunsPage(): React.ReactElement {
  const { runs, runsQuery } = useWorkspace();
  const [filter, setFilter] = useState<Filter>('all');
  const counts: Record<RunGroup, number> = {
    attention: 0,
    errored: 0,
    running: 0,
    success: 0,
  };
  for (const run of runs) {
    const group = runGroup(run);
    if (group !== null) {
      counts[group] += 1;
    }
  }
  const shown =
    filter === 'all' ? runs : runs.filter((run) => runGroup(run) === filter);
  const current = currentRun(runs);

  if (runsQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-faint">
        <Spinner label="Loading runs" className="size-4" />
        Loading runs
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <ErrorNotice error={runsQuery.error} />
      {current === null ? null : (
        <section aria-labelledby="current-run" className="space-y-3">
          <h2
            id="current-run"
            className="text-sm font-semibold text-text-strong"
          >
            Current run
          </h2>
          <CurrentRunCard run={current} />
        </section>
      )}
      <section aria-labelledby="run-list" className="space-y-3">
        <h2 id="run-list" className="text-sm font-semibold text-text-strong">
          Run list
        </h2>
        <Tabs<Filter>
          label="Run filters"
          value={filter}
          onChange={setFilter}
          tabs={[
            { id: 'all', label: 'All', count: runs.length },
            {
              id: 'attention',
              label: 'Needs attention',
              count: counts.attention,
            },
            { id: 'errored', label: 'Errored', count: counts.errored },
            { id: 'running', label: 'Running', count: counts.running },
            { id: 'success', label: 'Success', count: counts.success },
          ]}
        />
        {runs.length > 0 && shown.length === 0 ? (
          <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
            No runs match this filter.
          </p>
        ) : (
          <RunList runs={shown} emptyHint="Start one with New run." />
        )}
      </section>
    </div>
  );
}

/** The card for the run on top of the list. */
function CurrentRunCard({ run }: { run: Run }): React.ReactElement {
  return (
    <article className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-line border-l-4 border-l-accent bg-panel px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="rounded border border-accent-line bg-accent-soft px-1.5 text-[10px] font-semibold tracking-wide text-accent uppercase">
            Current
          </span>
          <Link
            to={runPath(run)}
            className="truncate text-sm font-medium text-text-strong hover:text-accent hover:underline"
          >
            {runTitle(run)}
          </Link>
        </div>
        <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-text-faint">
          <span className="font-mono">#{run.run_id}</span>
          <span aria-hidden="true">|</span>
          <span>{run.plan_only ? 'plan only run' : 'plan and apply run'}</span>
          <span aria-hidden="true">|</span>
          <span className="font-mono">{changeSummary(run)}</span>
        </p>
      </div>
      <StateBadge state={run.status} />
      <span
        className="text-xs whitespace-nowrap text-text-faint"
        title={formatDateTime(run.created_at)}
      >
        {formatRelative(run.created_at)}
      </span>
    </article>
  );
}
