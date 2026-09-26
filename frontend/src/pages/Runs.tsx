/** Every run across every workspace, newest first. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type Run } from '../api';
import { ErrorNotice, PageHeader, RunList, Spinner } from '../components';

/** The runs and the names of the workspaces they belong to. */
interface EveryRun {
  items: Run[];
  workspaceNames: Map<string, string>;
}

/**
 * Every run in the environment, newest first.
 *
 * The API lists runs one workspace at a time: `GET /runs` takes a required
 * `workspace_id` because the table is queried by its workspace index. So this
 * page reads the workspaces first and merges their runs, rather than asking for
 * an environment wide listing the backend does not serve.
 *
 * Each page already arrives newest first, so the merge is a stable interleave
 * on the run id, which is a ULID and so orders by creation time. A stable sort
 * leaves one workspace's runs in the order the API returned them.
 */
async function listEveryRun(signal: AbortSignal): Promise<EveryRun> {
  const workspaces = await api.listWorkspaces({ signal });
  const workspaceNames = new Map(
    workspaces.items.map((workspace) => [
      workspace.workspace_id,
      workspace.name,
    ])
  );
  const pages = await Promise.all(
    workspaces.items.map((workspace) =>
      api.listRuns({ workspace_id: workspace.workspace_id }, { signal })
    )
  );
  const items = pages.flatMap((page) => page.items);
  if (pages.length > 1) {
    items.sort((left, right) => right.run_id.localeCompare(left.run_id));
  }
  return { items, workspaceNames };
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
