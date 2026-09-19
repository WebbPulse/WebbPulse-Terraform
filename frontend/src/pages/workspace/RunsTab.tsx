/** The runs tab: every run on the workspace, newest first. */

import type { Run } from '../../api';
import { ErrorNotice, RunTable, Spinner } from '../../components';

/** Props for {@link RunsTab}. The page owns the query and hands its state down. */
export interface RunsTabProps {
  runs: Run[];
  isLoading: boolean;
  error: unknown;
}

/** The workspace's runs. */
export function RunsTab({
  runs,
  isLoading,
  error,
}: RunsTabProps): React.ReactElement {
  return (
    <div className="space-y-3">
      <ErrorNotice error={error} />
      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading runs" className="size-4" />
          Loading runs
        </div>
      ) : (
        <RunTable runs={runs} />
      )}
    </div>
  );
}
