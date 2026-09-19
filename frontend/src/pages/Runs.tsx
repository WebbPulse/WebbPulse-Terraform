/** Every run in the environment, newest first. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

import { api, type RunList } from '../api';
import { ErrorNotice, Spinner } from '../components';
import { RunTable } from '../components/RunTable';

/** The runs page. */
export function Runs(): React.ReactElement {
  const auth = useAuthClient();
  const query = usePolledQuery<RunList>(
    ({ signal }) => api.listRuns({}, { signal }),
    { intervalMs: 10_000, queryKey: 'runs', auth }
  );

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
