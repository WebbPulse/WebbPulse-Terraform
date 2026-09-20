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
import { ErrorNotice, PageHeader, Spinner, Tabs } from '../components';
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
    return (
      <div className="flex items-center gap-2 text-sm text-text-faint">
        <Spinner label="Loading the workspace" className="size-4" />
        Loading the workspace
      </div>
    );
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
      <PageHeader
        crumbs={[{ label: 'Workspaces', to: '/workspaces' }]}
        title={workspace.name}
        meta={
          settled ? (
            <span
              data-testid="workspace-status"
              className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                ready
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
                  : 'border-amber-500/40 bg-amber-500/10 text-amber-300'
              }`}
            >
              <span
                aria-hidden="true"
                className={`size-1.5 rounded-full ${ready ? 'bg-emerald-400' : 'bg-amber-400'}`}
              />
              {ready ? 'Ready' : 'Setup incomplete'}
            </span>
          ) : null
        }
        description={
          (workspace.description ?? '') === ''
            ? undefined
            : workspace.description
        }
        actions={
          <code className="font-mono text-xs text-text-faint">
            {workspaceId}
          </code>
        }
      />
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
      <Tabs
        label="Workspace sections"
        tabs={TABS}
        value={tab}
        onChange={setTab}
      />
      <div role="tabpanel" className="pt-1">
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
