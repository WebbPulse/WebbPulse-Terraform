/** The workspaces list, with the form that creates one. */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

import { api, type Engine, type WorkspaceList } from '../api';
import { ErrorNotice, Spinner } from '../components';

/** The refetch key the list reads and the create form invalidates. */
export const WORKSPACES_KEY = 'workspaces';

/** The engines a workspace can be created with. */
const ENGINES: readonly Engine[] = ['terraform', 'tofu'];

/** The workspaces list and its create form. */
export function Workspaces(): React.ReactElement {
  const auth = useAuthClient();
  const query = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }),
    { intervalMs: 30_000, queryKey: WORKSPACES_KEY, auth }
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-xl font-semibold text-surface-50">Workspaces</h1>
        {query.isFetching ? <Spinner label="Refreshing workspaces" /> : null}
      </div>
      <ErrorNotice error={query.error} />
      <CreateWorkspaceForm />
      {query.isLoading ? (
        <Spinner label="Loading workspaces" />
      ) : (
        <WorkspaceTable workspaces={query.data?.workspaces ?? []} />
      )}
    </div>
  );
}

/** The table of workspaces, or a sentence when there are none. */
function WorkspaceTable({
  workspaces,
}: {
  workspaces: WorkspaceList['workspaces'];
}): React.ReactElement {
  if (workspaces.length === 0) {
    return <p className="text-surface-300">No workspaces yet.</p>;
  }
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-surface-400">
        <tr>
          <th className="py-2">Name</th>
          <th className="py-2">Engine</th>
          <th className="py-2">Auto apply</th>
        </tr>
      </thead>
      <tbody>
        {workspaces.map((workspace) => (
          <tr
            key={workspace.workspace_id}
            className="border-t border-surface-700"
          >
            <td className="py-2">
              <Link
                to={`/workspaces/${workspace.workspace_id}`}
                className="text-brand-300 hover:text-brand-200"
              >
                {workspace.name}
              </Link>
            </td>
            <td className="py-2 text-surface-300">
              {workspace.engine} {workspace.engine_version}
            </td>
            <td className="py-2 text-surface-300">
              {workspace.auto_apply ? 'On' : 'Off'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The form that creates a workspace and refetches the list on success. */
function CreateWorkspaceForm(): React.ReactElement {
  const [name, setName] = useState('');
  const [engine, setEngine] = useState<Engine>('terraform');
  const [engineVersion, setEngineVersion] = useState('1.11.0');
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (body: { name: string; engine: Engine; engine_version: string }) =>
      api.createWorkspace(body),
    WORKSPACES_KEY
  );

  const submit = async (): Promise<void> => {
    try {
      await mutate({ name, engine, engine_version: engineVersion });
      setName('');
    } catch {
      return;
    }
  };

  return (
    <form
      aria-label="Create a workspace"
      className="flex flex-wrap items-end gap-3 rounded-lg border border-surface-700 bg-surface-800 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="text-sm">
        <span className="block text-surface-300">Name</span>
        <input
          required
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2"
        />
      </label>
      <label className="text-sm">
        <span className="block text-surface-300">Engine</span>
        <select
          value={engine}
          onChange={(event) => {
            setEngine(event.target.value as Engine);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2"
        >
          {ENGINES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm">
        <span className="block text-surface-300">Engine version</span>
        <input
          required
          value={engineVersion}
          onChange={(event) => {
            setEngineVersion(event.target.value);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2"
        />
      </label>
      <button
        type="submit"
        disabled={isMutating}
        className="flex items-center gap-2 rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {isMutating ? <Spinner label="Creating the workspace" /> : null}
        Create workspace
      </button>
      <ErrorNotice error={error} className="w-full" />
    </form>
  );
}
