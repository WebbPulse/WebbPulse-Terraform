/** The frame every workspace page shares: its header, its queries and its routes. */

import { useEffect, useState } from 'react';
import { Outlet, useParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  isConnected,
  type ConfigVersionList,
  type RunList,
  type Workspace,
} from '../api';
import {
  Button,
  CopyButton,
  ErrorNotice,
  PageHeader,
  Spinner,
  formatDateTime,
  formatRelative,
  useWorkspaceNav,
} from '../components';
import { NewRunDialog } from './workspace/NewRunDialog';
import { isSetupComplete, setupSteps } from './workspace/setup';
import { workspaceKeys, type WorkspaceContext } from './workspaceContext';

/** The workspace frame: header above, the routed section below. */
export function WorkspaceLayout(): React.ReactElement {
  const { workspaceId = '' } = useParams<{ workspaceId: string }>();
  const auth = useQueryAuth();
  const rail = useWorkspaceNav();
  const [newRun, setNewRun] = useState(false);
  const enabled = workspaceId !== '';
  const keys = workspaceKeys(workspaceId);
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
  const name = query.data?.name ?? null;
  const { setName } = rail;

  useEffect(() => {
    setName(name);
    return () => {
      setName(null);
    };
  }, [name, setName]);

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
  const context: WorkspaceContext = {
    workspace,
    versions: versionItems,
    runs: runItems,
    steps,
    settled,
    setupComplete: ready,
    versionsQuery: { isLoading: versions.isLoading, error: versions.error },
    runsQuery: { isLoading: runs.isLoading, error: runs.error },
    keys,
    openNewRun: () => {
      setNewRun(true);
    },
  };

  return (
    <div className="space-y-6">
      <WorkspaceHeader
        workspace={workspace}
        settled={settled}
        ready={ready}
        onNewRun={() => {
          setNewRun(true);
        }}
      />
      <ErrorNotice error={query.error} />
      <Outlet context={context} />
      <NewRunDialog
        open={newRun}
        onClose={() => {
          setNewRun(false);
        }}
        workspace={workspace}
        versions={versionItems}
        runsKey={keys.runs}
      />
    </div>
  );
}

/** The header every workspace page opens with. */
function WorkspaceHeader({
  workspace,
  settled,
  ready,
  onNewRun,
}: {
  workspace: Workspace;
  settled: boolean;
  ready: boolean;
  onNewRun: () => void;
}): React.ReactElement {
  const updated = workspace.updated_at ?? workspace.created_at;
  return (
    <div className="space-y-3 border-b border-line pb-4">
      <PageHeader
        crumbs={[{ label: 'Workspaces', to: '/workspaces' }]}
        title={workspace.name}
        meta={
          settled ? (
            <span
              data-testid="workspace-status"
              className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                ready
                  ? 'border-success-line bg-success-soft text-success'
                  : 'border-warning-line bg-warning-soft text-warning'
              }`}
            >
              <span
                aria-hidden="true"
                className={`size-1.5 rounded-full ${ready ? 'bg-success' : 'bg-warning'}`}
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
          <Button
            variant="primary"
            disabled={!isConnected(workspace)}
            title={
              isConnected(workspace)
                ? undefined
                : 'Connect an AWS account before starting a run.'
            }
            onClick={onNewRun}
          >
            + New run
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-x-1 gap-y-1 text-xs text-text-faint">
        <span>ID:</span>
        <code className="font-mono text-text-muted">
          {workspace.workspace_id}
        </code>
        <CopyButton
          value={workspace.workspace_id}
          subject="the workspace id"
          className="h-6 px-1.5"
        />
      </div>
      <dl className="flex flex-wrap items-center gap-x-2 text-xs text-text-muted">
        <Fact label={workspace.engine ?? 'terraform'}>
          <span className="font-mono">{workspace.engine_version}</span>
        </Fact>
        <Divider />
        <Fact label="AWS account">
          {isConnected(workspace) ? (
            <span className="font-mono">{workspace.run_role_account_id}</span>
          ) : (
            'Not connected'
          )}
        </Fact>
        <Divider />
        <Fact label="Updated">
          <span title={formatDateTime(updated)}>{formatRelative(updated)}</span>
        </Fact>
      </dl>
    </div>
  );
}

/** One fact in the header's meta line. */
function Fact({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="inline-flex items-center gap-1">
      <dt className="text-text-faint">{label}</dt>
      <dd className="text-text">{children}</dd>
    </div>
  );
}

/** The thin bar between two facts. */
function Divider(): React.ReactElement {
  return (
    <span aria-hidden="true" className="text-line-strong">
      |
    </span>
  );
}
