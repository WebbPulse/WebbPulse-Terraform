/** The workspaces list, with the way to create one. */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  accountStatus,
  accountStatusLabel,
  api,
  type Workspace,
  type WorkspaceList,
} from '../api';
import {
  EmptyState,
  ErrorNotice,
  INPUT_CLASS,
  buttonClass,
  PageHeader,
  RelativeTime,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
} from '../components';
import { useRunRoleCheck } from './useRunRoleCheck';
/** The refetch key the list reads and the create form invalidates. */
export const WORKSPACES_KEY = 'workspaces';

/** The workspaces list. */
export function Workspaces(): React.ReactElement {
  const auth = useQueryAuth();
  const [filter, setFilter] = useState('');
  const query = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }),
    { intervalMs: 30_000, queryKey: WORKSPACES_KEY, auth }
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title="Workspaces"
        description="Each workspace holds one root module, its variables and its runs."
        meta={
          query.isFetching && !query.isLoading ? (
            <Spinner label="Refreshing workspaces" className="size-3.5" />
          ) : null
        }
        actions={
          <Link to="/workspaces/new" className={buttonClass('primary')}>
            New workspace
          </Link>
        }
      />
      <ErrorNotice error={query.error} />
      {query.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading workspaces" className="size-4" />
          Loading workspaces
        </div>
      ) : (
        <WorkspaceTable
          workspaces={query.data?.items ?? []}
          filter={filter}
          onFilter={setFilter}
        />
      )}
    </div>
  );
}

/** The table of workspaces behind a name filter, or an invitation to create the first one. */
function WorkspaceTable({
  workspaces,
  filter,
  onFilter,
}: {
  workspaces: Workspace[];
  filter: string;
  onFilter: (value: string) => void;
}): React.ReactElement {
  if (workspaces.length === 0) {
    return (
      <EmptyState
        title="No workspaces yet."
        hint="Connect a repository, or upload configuration from the CLI or the API."
        action={
          <Link to="/workspaces/new" className={buttonClass('primary')}>
            Create the first workspace
          </Link>
        }
      />
    );
  }
  const needle = filter.trim().toLowerCase();
  const shown =
    needle === ''
      ? workspaces
      : workspaces.filter((workspace) =>
          workspace.name.toLowerCase().includes(needle)
        );
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <input
          type="search"
          aria-label="Filter workspaces by name"
          placeholder="Filter workspaces by name"
          value={filter}
          onChange={(event) => {
            onFilter(event.target.value);
          }}
          className={`${INPUT_CLASS} max-w-xs`}
        />
        <span className="text-xs text-text-faint">
          {shown.length} of {workspaces.length}
        </span>
      </div>
      {shown.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
          No workspaces match that name.
        </p>
      ) : (
        <Table label="Workspaces">
          <thead>
            <tr>
              <Th>Workspace name</Th>
              <Th>Engine</Th>
              <Th>AWS account</Th>
              <Th>Latest change</Th>
            </tr>
          </thead>
          <tbody>
            {shown.map((workspace) => (
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
                  <RelativeTime
                    iso={workspace.updated_at ?? workspace.created_at}
                  />
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
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
