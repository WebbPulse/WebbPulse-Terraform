/** The workspaces list, with the dialog that creates one. */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  isConnected,
  type Engine,
  type Workspace,
  type WorkspaceCreate,
  type WorkspaceList,
} from '../api';
import {
  Button,
  Dialog,
  ErrorNotice,
  Spinner,
  formatRelative,
} from '../components';

/** The refetch key the list reads and the create form invalidates. */
export const WORKSPACES_KEY = 'workspaces';

/** The engines a workspace can be created with. */
const ENGINES: readonly Engine[] = ['terraform', 'tofu'];

/** The engine version a new workspace starts on. */
const DEFAULT_ENGINE_VERSION = '1.11.0';

/** The workspaces list and its create dialog. */
export function Workspaces(): React.ReactElement {
  const auth = useQueryAuth();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const query = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }),
    { intervalMs: 30_000, queryKey: WORKSPACES_KEY, auth }
  );

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-surface-50">Workspaces</h1>
          {query.isFetching && !query.isLoading ? (
            <Spinner label="Refreshing workspaces" className="size-3.5" />
          ) : null}
        </div>
        <Button
          variant="primary"
          onClick={() => {
            setCreating(true);
          }}
        >
          New workspace
        </Button>
      </div>
      <ErrorNotice error={query.error} />
      {query.isLoading ? (
        <Spinner label="Loading workspaces" />
      ) : (
        <WorkspaceTable
          workspaces={query.data?.items ?? []}
          onCreate={() => {
            setCreating(true);
          }}
        />
      )}
      <Dialog
        open={creating}
        onClose={() => {
          setCreating(false);
        }}
        title="New workspace"
        description="Name it now. Connecting an AWS account comes next, on the workspace page."
      >
        <CreateWorkspaceForm
          onCancel={() => {
            setCreating(false);
          }}
          onCreated={(workspace) => {
            setCreating(false);
            void navigate(`/workspaces/${workspace.workspace_id}`);
          }}
        />
      </Dialog>
    </div>
  );
}

/** The table of workspaces, or an invitation to create the first one. */
function WorkspaceTable({
  workspaces,
  onCreate,
}: {
  workspaces: Workspace[];
  onCreate: () => void;
}): React.ReactElement {
  if (workspaces.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-surface-700 px-4 py-10 text-center">
        <p className="text-sm text-surface-200">No workspaces yet.</p>
        <p className="mt-1 text-xs text-surface-400">
          A workspace holds one root module, its variables and its runs.
        </p>
        <Button variant="primary" className="mt-4" onClick={onCreate}>
          Create the first workspace
        </Button>
      </div>
    );
  }
  return (
    <div className="overflow-hidden rounded-lg border border-surface-700">
      <table className="w-full text-left text-sm">
        <thead className="bg-surface-800 text-xs text-surface-400">
          <tr>
            <th className="px-3 py-2 font-medium">Name</th>
            <th className="px-3 py-2 font-medium">Engine</th>
            <th className="px-3 py-2 font-medium">AWS account</th>
            <th className="px-3 py-2 font-medium">Created</th>
          </tr>
        </thead>
        <tbody>
          {workspaces.map((workspace) => (
            <tr
              key={workspace.workspace_id}
              className="border-t border-surface-700 hover:bg-surface-800/60"
            >
              <td className="px-3 py-2">
                <Link
                  to={`/workspaces/${workspace.workspace_id}`}
                  className="font-medium text-surface-50 hover:text-brand-300"
                >
                  {workspace.name}
                </Link>
                {(workspace.description ?? '') === '' ? null : (
                  <p className="truncate text-xs text-surface-400">
                    {workspace.description}
                  </p>
                )}
              </td>
              <td className="px-3 py-2 text-surface-300">
                {workspace.engine ?? 'terraform'} {workspace.engine_version}
              </td>
              <td className="px-3 py-2">
                <ConnectionCell workspace={workspace} />
              </td>
              <td className="px-3 py-2 text-surface-400">
                {formatRelative(workspace.created_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The account id with a green dot, or the words for a missing connection. */
function ConnectionCell({
  workspace,
}: {
  workspace: Workspace;
}): React.ReactElement {
  if (isConnected(workspace)) {
    return (
      <span className="inline-flex items-center gap-2 font-mono text-xs text-surface-200">
        <span
          aria-hidden="true"
          className="inline-block size-2 rounded-full bg-emerald-400"
        />
        {workspace.run_role_account_id}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2 text-xs text-surface-400">
      <span
        aria-hidden="true"
        className="inline-block size-2 rounded-full bg-surface-600"
      />
      {(workspace.run_role_arn ?? null) === null
        ? 'Not connected'
        : 'Not checked'}
    </span>
  );
}

/** The form that creates a workspace with a name and its engine. */
function CreateWorkspaceForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (workspace: Workspace) => void;
}): React.ReactElement {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [engine, setEngine] = useState<Engine>('terraform');
  const [engineVersion, setEngineVersion] = useState(DEFAULT_ENGINE_VERSION);
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (body: WorkspaceCreate) => api.createWorkspace(body),
    WORKSPACES_KEY
  );

  const submit = async (): Promise<void> => {
    const body: WorkspaceCreate = {
      name: name.trim(),
      engine,
      engine_version: engineVersion.trim(),
    };
    if (description.trim() !== '') {
      body.description = description.trim();
    }
    try {
      onCreated(await mutate(body));
    } catch {
      return;
    }
  };

  return (
    <form
      aria-label="Create a workspace"
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="block text-sm">
        <span className="text-surface-300">Name</span>
        <input
          required
          autoFocus
          value={name}
          pattern="[A-Za-z0-9][A-Za-z0-9._\-]*"
          title="Letters, digits, dots, underscores and hyphens, starting with a letter or digit."
          onChange={(event) => {
            setName(event.target.value);
          }}
          className={INPUT}
        />
      </label>
      <label className="block text-sm">
        <span className="text-surface-300">Description</span>
        <input
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
          className={INPUT}
        />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block text-sm">
          <span className="text-surface-300">Engine</span>
          <select
            value={engine}
            onChange={(event) => {
              setEngine(event.target.value as Engine);
            }}
            className={INPUT}
          >
            {ENGINES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm">
          <span className="text-surface-300">Engine version</span>
          <input
            required
            value={engineVersion}
            onChange={(event) => {
              setEngineVersion(event.target.value);
            }}
            className={`${INPUT} font-mono`}
          />
        </label>
      </div>
      <ErrorNotice error={error} />
      <div className="flex justify-end gap-2 pt-1">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Creating the workspace"
        >
          Create workspace
        </Button>
      </div>
    </form>
  );
}

/** The input styling the dialog's fields share. */
const INPUT =
  'mt-1 h-8 w-full rounded-md border border-surface-600 bg-surface-900 px-2.5 text-sm text-surface-100 focus-visible:border-brand-400 focus-visible:ring-1 focus-visible:ring-brand-400 focus-visible:outline-none';
