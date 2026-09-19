/** The runs tab: every run on the workspace, newest first. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

import { api, type RunList } from '../../api';
import { ErrorNotice, Spinner } from '../../components';
import { RunTable } from '../../components/RunTable';

/** Props for {@link RunsTab}. */
export interface RunsTabProps {
  workspaceId: string;
}

/** The workspace's runs. */
export function RunsTab({ workspaceId }: RunsTabProps): React.ReactElement {
  const auth = useAuthClient();
  const query = usePolledQuery<RunList>(
    ({ signal }) => api.listRuns({ workspace_id: workspaceId }, { signal }),
    { intervalMs: 10_000, queryKey: `runs:${workspaceId}`, auth }
  );

  return (
    <div className="space-y-3">
      <ErrorNotice error={query.error} />
      {query.isLoading ? (
        <Spinner label="Loading runs" />
      ) : (
        <RunTable runs={query.data?.items ?? []} />
      )}
    </div>
  );
}
