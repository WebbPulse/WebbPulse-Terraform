/** Every run across every workspace, newest first. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

import { api, type Run, type RunList } from '../api';
import { ErrorNotice, Spinner } from '../components';
import { RunTable } from '../components/RunTable';

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
async function listEveryRun(signal: AbortSignal): Promise<RunList> {
  const workspaces = await api.listWorkspaces({ signal });
  const pages = await Promise.all(
    workspaces.items.map((workspace) =>
      api.listRuns({ workspace_id: workspace.workspace_id }, { signal })
    )
  );
  if (pages.length <= 1) {
    return { items: pages.flatMap((page) => page.items) };
  }
  const items = pages
    .flatMap((page) => page.items)
    .sort((left: Run, right: Run) => right.run_id.localeCompare(left.run_id));
  return { items };
}

/** The runs page. */
export function Runs(): React.ReactElement {
  const auth = useAuthClient();
  const query = usePolledQuery<RunList>(({ signal }) => listEveryRun(signal), {
    intervalMs: 10_000,
    queryKey: 'runs',
    auth,
  });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold text-surface-50">Runs</h1>
      <ErrorNotice error={query.error} />
      {query.isLoading ? (
        <Spinner label="Loading runs" />
      ) : (
        <RunTable runs={query.data?.items ?? []} />
      )}
    </div>
  );
}
