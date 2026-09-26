/** The workspace overview: the setup checklist until it is done, then the latest run. */

import { Link } from 'react-router-dom';

import { isActive, isConnected, type Run, type Workspace } from '../../api';
import {
  Button,
  EmptyState,
  RelativeTime,
  StateBadge,
  changeSummary,
  elapsedBetween,
  formatDuration,
  runPath,
  runTitle,
  useNow,
} from '../../components';
import { useWorkspace } from '../workspaceContext';
import { SetupChecklist } from './SetupChecklist';
import { latestUploadedVersion } from './setup';

/** The overview page. */
export function OverviewPage(): React.ReactElement {
  const {
    workspace,
    versions,
    runs,
    steps,
    settled,
    setupComplete,
    keys,
    openNewRun,
  } = useWorkspace();
  const latest = runs[0] ?? null;
  const base = `/workspaces/${workspace.workspace_id}`;

  return (
    <div className="space-y-6">
      {settled && !setupComplete ? (
        <SetupChecklist
          workspace={workspace}
          steps={steps}
          versions={versions}
          runs={runs}
          keys={keys}
        />
      ) : null}
      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <section aria-labelledby="latest-run" className="space-y-3">
          <div className="flex items-baseline justify-between gap-3">
            <h2
              id="latest-run"
              className="text-sm font-semibold text-text-strong"
            >
              Latest run
            </h2>
            <Link
              to={`${base}/runs`}
              className="text-xs text-accent hover:text-accent-hover hover:underline"
            >
              View all runs
            </Link>
          </div>
          {latest === null ? (
            <EmptyState
              title="No runs yet."
              hint="Start one with New run, or from a configuration version."
              action={
                isConnected(workspace) ? (
                  <Button variant="primary" onClick={openNewRun}>
                    + New run
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <LatestRunCard run={latest} />
          )}
        </section>
        <WorkspaceFacts
          workspace={workspace}
          latestVersion={
            latestUploadedVersion(versions)?.config_version_id ?? null
          }
        />
      </div>
    </div>
  );
}

/** The latest run, with its duration and resource counts. */
function LatestRunCard({ run }: { run: Run }): React.ReactElement {
  const active = isActive(run.status);
  const now = useNow(active);
  const elapsed = elapsedBetween(run.started_at, run.finished_at, now);
  return (
    <article className="rounded-lg border border-line bg-panel">
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
        <div className="min-w-0">
          <Link
            to={runPath(run)}
            className="block truncate text-sm font-medium text-text-strong hover:text-accent hover:underline"
          >
            {runTitle(run)}
          </Link>
          <p className="mt-0.5 text-xs text-text-faint">
            {run.plan_only ? 'Plan only run' : 'Plan and apply run'} created{' '}
            <RelativeTime iso={run.created_at} />
          </p>
        </div>
        <StateBadge state={run.status} />
      </div>
      <dl className="grid grid-cols-2 divide-x divide-line border-t border-line sm:grid-cols-3">
        <Metric label="Plan and apply duration">
          {elapsed === null ? 'Not started' : formatDuration(elapsed)}
        </Metric>
        <Metric label="Resources changed">
          <span className="font-mono">{changeSummary(run)}</span>
        </Metric>
        <Metric label="Run id">
          <span className="font-mono text-xs">{run.run_id}</span>
        </Metric>
      </dl>
      <div className="border-t border-line px-4 py-3">
        <Link
          to={runPath(run)}
          className="inline-flex h-8 items-center rounded-md border border-line-strong bg-panel px-3 text-sm text-text-strong shadow-xs hover:bg-raised"
        >
          See details
        </Link>
      </div>
    </article>
  );
}

/** One figure in a metric row. */
function Metric({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="min-w-0 px-4 py-3">
      <dt className="text-xs text-text-faint">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-text">{children}</dd>
    </div>
  );
}

/** The workspace's settings at a glance, each linking to where it is changed. */
function WorkspaceFacts({
  workspace,
  latestVersion,
}: {
  workspace: Workspace;
  latestVersion: string | null;
}): React.ReactElement {
  const base = `/workspaces/${workspace.workspace_id}`;
  const rows: { label: string; value: React.ReactNode; to: string }[] = [
    {
      label: 'Engine',
      value: `${workspace.engine ?? 'terraform'} ${workspace.engine_version}`,
      to: `${base}/settings/general`,
    },
    {
      label: 'Working directory',
      value: (
        <span className="font-mono">{workspace.working_directory ?? '.'}</span>
      ),
      to: `${base}/settings/general`,
    },
    {
      label: 'AWS account',
      value: isConnected(workspace) ? (
        <span className="font-mono">{workspace.run_role_account_id}</span>
      ) : (
        'Not connected'
      ),
      to: `${base}/settings/run-role`,
    },
    {
      label: 'Latest configuration',
      value:
        latestVersion === null ? (
          'None uploaded'
        ) : (
          <span className="font-mono text-xs">{latestVersion}</span>
        ),
      to: `${base}/configuration-versions`,
    },
    {
      label: 'Created',
      value: <RelativeTime iso={workspace.created_at} />,
      to: `${base}/settings/general`,
    },
  ];
  return (
    <aside
      aria-label="Workspace details"
      className="lg:sticky lg:top-6 lg:self-start"
    >
      <dl className="divide-y divide-line border-y border-line">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex items-baseline justify-between gap-3 py-2 text-xs"
          >
            <dt className="text-text-faint">{row.label}</dt>
            <dd className="min-w-0 truncate text-right text-text">
              <Link to={row.to} className="hover:text-accent hover:underline">
                {row.value}
              </Link>
            </dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}
