/** The runs tab: every run on the workspace, newest first. */

import type { Run } from '../../api';
import { ErrorNotice, Spinner } from '../../components';
import { RunTable } from '../../components/RunTable';

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
      {isLoading ? <Spinner label="Loading runs" /> : <RunTable runs={runs} />}
    </div>
  );
}
