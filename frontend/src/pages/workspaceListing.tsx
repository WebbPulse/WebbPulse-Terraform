/**
 * The pieces the workspaces list and a project page share: the sort, the name
 * search and the table of workspaces, so both read alike and sort alike.
 */

import { Link } from 'react-router-dom';

import {
  accountStatus,
  accountStatusLabel,
  runStateLabel,
  type Workspace,
  type WorkspaceListItem,
  type WorkspaceSort,
} from '../api';
import {
  INPUT_CLASS,
  RelativeTime,
  runPath,
  StateBadge,
  Table,
  Td,
  Th,
  Tr,
} from '../components';
import { useRunRoleCheck } from './useRunRoleCheck';
import { readSort, WORKSPACE_SORTS } from './workspaceListingRules';

/** The sort control. */
export function SortSelect({
  value,
  onChange,
}: {
  value: WorkspaceSort;
  onChange: (value: WorkspaceSort) => void;
}): React.ReactElement {
  return (
    <label className="flex items-center gap-2 text-xs text-text-faint">
      Sort by
      <select
        aria-label="Sort workspaces"
        value={value}
        onChange={(event) => {
          onChange(readSort(event.target.value));
        }}
        className={`${INPUT_CLASS} mt-0! w-auto!`}
      >
        {WORKSPACE_SORTS.map((choice) => (
          <option key={choice.id} value={choice.id}>
            {choice.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** The name search box. */
export function NameSearch({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}): React.ReactElement {
  return (
    <input
      type="search"
      aria-label="Filter workspaces by name"
      placeholder="Filter workspaces by name"
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
      }}
      className={`${INPUT_CLASS} mt-0! max-w-xs`}
    />
  );
}

/** A table of workspaces in the order given, each with its latest run. */
export function WorkspaceRows({
  workspaces,
  label = 'Workspaces',
}: {
  workspaces: readonly WorkspaceListItem[];
  label?: string;
}): React.ReactElement {
  return (
    <Table label={label}>
      <thead>
        <tr>
          <Th>Workspace name</Th>
          <Th>Run status</Th>
          <Th>Engine</Th>
          <Th>AWS account</Th>
          <Th>Latest change</Th>
        </tr>
      </thead>
      <tbody>
        {workspaces.map((workspace) => (
          <Tr key={workspace.workspace_id}>
            <Td>
              <Link
                to={`/workspaces/${workspace.workspace_id}`}
                className="font-medium text-text-strong hover:text-accent hover:underline"
              >
                {workspace.name}
              </Link>
              {(workspace.description ?? '') === '' ? null : (
                <p className="max-w-md truncate text-xs text-text-faint">
                  {workspace.description}
                </p>
              )}
            </Td>
            <Td>
              <RunStatusCell workspace={workspace} />
            </Td>
            <Td className="text-text-muted">
              {workspace.engine ?? 'terraform'}{' '}
              <span className="font-mono text-xs">
                {workspace.engine_version}
              </span>
            </Td>
            <Td>
              <ConnectionCell workspace={workspace} />
            </Td>
            <Td className="text-xs whitespace-nowrap text-text-faint">
              <RelativeTime iso={workspace.latest_change_at} />
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/**
 * The newest run's state as a badge linking to that run, as HCP Terraform's
 * list shows it, or the words for a workspace that never ran.
 */
export function RunStatusCell({
  workspace,
}: {
  workspace: WorkspaceListItem;
}): React.ReactElement {
  const run = workspace.latest_run;
  if (run === null || run === undefined) {
    return (
      <span
        data-testid="workspace-row-run-status"
        className="text-xs text-text-faint"
      >
        No runs yet
      </span>
    );
  }
  return (
    <Link
      to={runPath({
        run_id: run.run_id,
        workspace_id: workspace.workspace_id,
      })}
      data-testid="workspace-row-run-status"
      aria-label={`Latest run: ${runStateLabel(run.status)}`}
      className="inline-flex rounded-full"
    >
      <StateBadge
        state={run.status}
        className="transition-opacity hover:opacity-80"
      />
    </Link>
  );
}

/**
 * The account id with a green dot, or the words for a missing connection.
 *
 * Each row with a role reads the same check the workspace header does, so the
 * list never disagrees with the page it links to.
 */
function ConnectionCell({
  workspace,
}: {
  workspace: Workspace;
}): React.ReactElement {
  const check = useRunRoleCheck(workspace);
  const status = accountStatus(workspace, check);
  if (status.state === 'connected') {
    return (
      <span
        data-testid="workspace-row-account"
        data-connection={status.state}
        className="inline-flex items-center gap-2 font-mono text-xs text-text"
      >
        <span
          aria-hidden="true"
          className="inline-block size-2 rounded-full bg-success"
        />
        {accountStatusLabel(status)}
      </span>
    );
  }
  return (
    <span
      data-testid="workspace-row-account"
      data-connection={status.state}
      className="inline-flex items-center gap-2 text-xs text-text-faint"
    >
      <span
        aria-hidden="true"
        className={`inline-block size-2 rounded-full ${
          status.state === 'failed' ? 'bg-danger' : 'bg-surface-400'
        }`}
      />
      {accountStatusLabel(status)}
    </span>
  );
}
