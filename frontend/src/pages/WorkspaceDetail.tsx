/** The workspace detail page: its setup checklist and its four tabs. */

import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type ConfigVersionList,
  type RunList,
  type Workspace,
} from '../api';
import { ErrorNotice, Spinner } from '../components';
import { ConfigVersionsTab } from './workspace/ConfigVersionsTab';
import { OverviewTab } from './workspace/OverviewTab';
import { RunsTab } from './workspace/RunsTab';
import { SetupChecklist } from './workspace/SetupChecklist';
import { VariablesTab } from './workspace/VariablesTab';
import { isSetupComplete, setupSteps } from './workspace/setup';

/** Which tab is showing. */
type Tab = 'overview' | 'variables' | 'config-versions' | 'runs';

/** The tabs in the order they are rendered. */
const TABS: readonly { id: Tab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'variables', label: 'Variables' },
  { id: 'config-versions', label: 'Configuration versions' },
  { id: 'runs', label: 'Runs' },
];

/** The workspace detail page. */
export function WorkspaceDetail(): React.ReactElement {
  const { workspaceId = '' } = useParams<{ workspaceId: string }>();
  const auth = useQueryAuth();
  const [tab, setTab] = useState<Tab>('overview');
  const enabled = workspaceId !== '';
  const keys = {
    workspace: `workspace:${workspaceId}`,
    versions: `config-versions:${workspaceId}`,
    runs: `runs:${workspaceId}`,
  };
  const query = usePolledQuery<Workspace>(
    ({ signal }) => api.getWorkspace(workspaceId, { signal }),
    { intervalMs: 60_000, queryKey: keys.workspace, auth, enabled }
  );
  const versions = usePolledQuery<ConfigVersionList>(
    ({ signal }) => api.listConfigVersions(workspaceId, { signal }),
    { intervalMs: 30_000, queryKey: keys.versions, auth, enabled }
  );
  const runs = usePolledQuery<RunList>(
    ({ signal }) => api.listRuns({ workspace_id: workspaceId }, { signal }),
    { intervalMs: 10_000, queryKey: keys.runs, auth, enabled }
  );

  if (query.isLoading) {
    return <Spinner label="Loading the workspace" />;
  }
  if (query.data === null) {
    return (
      <ErrorNotice error={query.error ?? new Error('Workspace not found.')} />
    );
  }

  const workspace = query.data;
  const versionItems = versions.data?.items ?? [];
  const runItems = runs.data?.items ?? [];
  const steps = setupSteps(workspace, versionItems, runItems);
  const settled = versions.data !== null && runs.data !== null;
  const ready = settled && isSetupComplete(steps);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <h1 className="truncate text-xl font-semibold text-surface-50">
              {workspace.name}
            </h1>
            {settled ? (
              <span
                data-testid="workspace-status"
                className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                  ready
                    ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                    : 'border-amber-500/40 bg-amber-500/10 text-amber-300'
                }`}
              >
                {ready ? 'Ready' : 'Setup incomplete'}
              </span>
            ) : null}
          </div>
          {(workspace.description ?? '') === '' ? null : (
            <p className="mt-0.5 text-sm text-surface-300">
              {workspace.description}
            </p>
          )}
          <p className="mt-0.5 font-mono text-xs text-surface-500">
            {workspaceId}
          </p>
        </div>
      </div>
      <ErrorNotice error={query.error} />
      {settled && !ready ? (
        <SetupChecklist
          workspace={workspace}
          steps={steps}
          versions={versionItems}
          runs={runItems}
          keys={keys}
        />
      ) : null}
      <div
        role="tablist"
        aria-label="Workspace sections"
        className="flex gap-1 border-b border-surface-700"
      >
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            onClick={() => {
              setTab(entry.id);
            }}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition-colors focus-visible:ring-2 focus-visible:ring-brand-400 focus-visible:outline-none ${
              tab === entry.id
                ? 'border-brand-400 text-surface-50'
                : 'border-transparent text-surface-300 hover:text-surface-50'
            }`}
          >
            {entry.label}
          </button>
        ))}
      </div>
      <div role="tabpanel">
        {tab === 'overview' ? (
          <OverviewTab
            workspace={workspace}
            queryKey={keys.workspace}
            setupComplete={ready}
          />
        ) : null}
        {tab === 'variables' ? (
          <VariablesTab workspaceId={workspaceId} />
        ) : null}
        {tab === 'config-versions' ? (
          <ConfigVersionsTab
            workspace={workspace}
            versions={versionItems}
            isLoading={versions.isLoading}
            error={versions.error}
            keys={keys}
          />
        ) : null}
        {tab === 'runs' ? (
          <RunsTab
            runs={runItems}
            isLoading={runs.isLoading}
            error={runs.error}
          />
        ) : null}
      </div>
    </div>
  );
}
