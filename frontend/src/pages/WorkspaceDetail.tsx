/** The workspace detail page and its four tabs. */

import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

import { api, type Workspace } from '../api';
import { ErrorNotice, Spinner } from '../components';
import { ConfigVersionsTab } from './workspace/ConfigVersionsTab';
import { OverviewTab } from './workspace/OverviewTab';
import { RunsTab } from './workspace/RunsTab';
import { VariablesTab } from './workspace/VariablesTab';

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
  const auth = useAuthClient();
  const [tab, setTab] = useState<Tab>('overview');
  const queryKey = `workspace:${workspaceId}`;
  const query = usePolledQuery<Workspace>(
    ({ signal }) => api.getWorkspace(workspaceId, { signal }),
    { intervalMs: 60_000, queryKey, auth, enabled: workspaceId !== '' }
  );

  if (query.isLoading) {
    return <Spinner label="Loading the workspace" />;
  }
  if (query.data === null) {
    return (
      <ErrorNotice error={query.error ?? new Error('Workspace not found.')} />
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-surface-50">
          {query.data.name}
        </h1>
        <p className="font-mono text-sm text-surface-400">{workspaceId}</p>
      </div>
      <ErrorNotice error={query.error} />
      <div role="tablist" className="flex gap-1 border-b border-surface-700">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={tab === entry.id}
            onClick={() => {
              setTab(entry.id);
            }}
            className={`rounded-t-md px-3 py-2 text-sm ${
              tab === entry.id
                ? 'bg-surface-800 text-surface-50'
                : 'text-surface-300 hover:text-white'
            }`}
          >
            {entry.label}
          </button>
        ))}
      </div>
      <div role="tabpanel">
        {tab === 'overview' ? (
          <OverviewTab workspace={query.data} queryKey={queryKey} />
        ) : null}
        {tab === 'variables' ? (
          <VariablesTab workspaceId={workspaceId} />
        ) : null}
        {tab === 'config-versions' ? (
          <ConfigVersionsTab workspaceId={workspaceId} />
        ) : null}
        {tab === 'runs' ? <RunsTab workspaceId={workspaceId} /> : null}
      </div>
    </div>
  );
}
