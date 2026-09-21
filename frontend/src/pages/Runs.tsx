/** Every run across every workspace, newest first. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type Run } from '../api';
import { listRunHistory } from '../api/runHistory';
import { ErrorNotice, PageHeader, RunList, Spinner } from '../components';

/** The runs and the names of the workspaces they belong to. */
interface EveryRun {
  items: Run[];
  workspaceNames: Map<string, string>;
}

/** Load global history and workspace labels concurrently. */
async function listEveryRun(signal: AbortSignal): Promise<EveryRun> {
  const [workspaces, history] = await Promise.all([
    api.listWorkspaces({ signal }),
    listRunHistory({}, signal),
  ]);
  const workspaceNames = new Map(
    workspaces.items.map((workspace) => [
      workspace.workspace_id,
      workspace.name,
    ])
  );
  return { items: history.items, workspaceNames };
}

/** The runs page. */
export function Runs(): React.ReactElement {
  const auth = useQueryAuth();
  const query = usePolledQuery<EveryRun>(({ signal }) => listEveryRun(signal), {
    intervalMs: 10_000,
    queryKey: 'runs',
    auth,
  });

  return (
    <div className="space-y-5">
      <PageHeader
        title="Runs"
        description="Every plan and apply across your workspaces, newest first."
      />
      <ErrorNotice error={query.error} />
      {query.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading runs" className="size-4" />
          Loading runs
        </div>
      ) : (
        <RunList
          runs={query.data?.items ?? []}
          workspaceNames={query.data?.workspaceNames ?? new Map()}
        />
      )}
    </div>
  );
}
