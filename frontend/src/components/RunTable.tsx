/** The runs table, shared by the runs page and the workspace's runs tab. */

import { Link } from 'react-router-dom';

import type { Run } from '../api';
import { EmptyState } from './EmptyState';
import { formatDateTime, formatRelative, shortRunId } from './format';
import { StateBadge } from './StateBadge';
import { Table, Td, Th, Tr } from './Table';

/** Props for {@link RunTable}. */
export interface RunTableProps {
  runs: Run[];
  /** Workspace names by id, when the table spans workspaces. */
  workspaceNames?: ReadonlyMap<string, string>;
}

/** The plan counts as a single line, or a hyphen when the plan has not reported. */
function changeSummary(run: Run): string {
  if (run.changes === null || run.changes === undefined) {
    return '-';
  }
  const { add, change, destroy } = run.changes;
  return `+${String(add)} ~${String(change)} -${String(destroy)}`;
}

/** A table of runs, or a sentence when there are none. */
export function RunTable({
  runs,
  workspaceNames,
}: RunTableProps): React.ReactElement {
  if (runs.length === 0) {
    return (
      <EmptyState
        title="No runs yet."
        hint="Start one from a configuration version on a workspace."
      />
    );
  }
  return (
    <Table label="Runs">
      <thead>
        <tr>
          <Th>Run</Th>
          {workspaceNames === undefined ? null : <Th>Workspace</Th>}
          <Th>State</Th>
          <Th>Changes</Th>
          <Th>Mode</Th>
          <Th>Started</Th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <Tr key={run.run_id}>
            <Td>
              <Link
                to={`/runs/${run.run_id}`}
                title={run.run_id}
                className="font-mono text-xs text-text-strong hover:text-brand-300"
              >
                {shortRunId(run.run_id)}
              </Link>
              {run.message === undefined || run.message === '' ? null : (
                <p className="max-w-xs truncate text-xs text-text-faint">
                  {run.message}
                </p>
              )}
            </Td>
            {workspaceNames === undefined ? null : (
              <Td>
                <Link
                  to={`/workspaces/${run.workspace_id}`}
                  className="text-text hover:text-brand-300"
                >
                  {workspaceNames.get(run.workspace_id) ?? run.workspace_id}
                </Link>
              </Td>
            )}
            <Td>
              <StateBadge state={run.status} />
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {changeSummary(run)}
            </Td>
            <Td className="text-xs text-text-muted">
              {run.plan_only ? 'Plan only' : 'Plan and apply'}
            </Td>
            <Td
              className="text-xs whitespace-nowrap text-text-faint"
              title={formatDateTime(run.created_at)}
            >
              {formatRelative(run.created_at)}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}
