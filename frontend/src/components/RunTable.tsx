/** The runs table, shared by the runs page and the workspace's runs tab. */

import { Link } from 'react-router-dom';

import type { Run } from '../api';
import { StateBadge } from './StateBadge';

/** Props for {@link RunTable}. */
export interface RunTableProps {
  runs: Run[];
}

/** The plan counts as a single line, or an em dash free placeholder. */
function changeSummary(run: Run): string {
  if (run.changes === null || run.changes === undefined) {
    return '-';
  }
  const { add, change, destroy } = run.changes;
  return `+${String(add)} ~${String(change)} -${String(destroy)}`;
}

/** A table of runs, or a sentence when there are none. */
export function RunTable({ runs }: RunTableProps): React.ReactElement {
  if (runs.length === 0) {
    return <p className="text-surface-300">No runs yet.</p>;
  }
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-surface-400">
        <tr>
          <th className="py-2">Run</th>
          <th className="py-2">State</th>
          <th className="py-2">Changes</th>
          <th className="py-2">Started</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.run_id} className="border-t border-surface-700">
            <td className="py-2">
              <Link
                to={`/runs/${run.run_id}`}
                className="font-mono text-brand-300 hover:text-brand-200"
              >
                {run.run_id}
              </Link>
            </td>
            <td className="py-2">
              <StateBadge state={run.status} />
            </td>
            <td className="py-2 font-mono text-surface-300">
              {changeSummary(run)}
            </td>
            <td className="py-2 text-surface-300">{run.created_at}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
