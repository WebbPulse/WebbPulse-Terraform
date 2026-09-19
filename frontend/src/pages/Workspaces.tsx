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
  EmptyState,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
  formatDateTime,
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
      <PageHeader
        title="Workspaces"
        description="Each workspace holds one root module, its variables and its runs."
        meta={
          query.isFetching && !query.isLoading ? (
            <Spinner label="Refreshing workspaces" className="size-3.5" />
          ) : null
        }
        actions={
          <Button
            variant="primary"
            onClick={() => {
              setCreating(true);
            }}
          >
            New workspace
          </Button>
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
      <EmptyState
        title="No workspaces yet."
        hint="A workspace holds one root module, its variables and its runs."
        action={
          <Button variant="primary" onClick={onCreate}>
            Create the first workspace
          </Button>
        }
      />
    );
  }
  return (
    <Table label="Workspaces">
      <thead>
        <tr>
          <Th>Name</Th>
          <Th>Engine</Th>
          <Th>AWS account</Th>
          <Th>Created</Th>
        </tr>
      </thead>
      <tbody>
        {workspaces.map((workspace) => (
          <Tr key={workspace.workspace_id}>
            <Td>
              <Link
                to={`/workspaces/${workspace.workspace_id}`}
                className="font-medium text-text-strong hover:text-brand-300"
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
            <Td
              className="text-xs whitespace-nowrap text-text-faint"
              title={formatDateTime(workspace.created_at)}
            >
              {formatRelative(workspace.created_at)}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
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
      <span className="inline-flex items-center gap-2 font-mono text-xs text-text">
        <span
          aria-hidden="true"
          className="inline-block size-2 rounded-full bg-emerald-400"
        />
        {workspace.run_role_account_id}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2 text-xs text-text-faint">
      <span
        aria-hidden="true"
        className="inline-block size-2 rounded-full bg-surface-600"
      />
      {workspace.run_role_arn === null ? 'Not connected' : 'Not checked'}
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
      <Field
        label="Name"
        hint="Letters, digits, dots, underscores and hyphens."
      >
        {(control) => (
          <input
            {...control}
            required
            autoFocus
            value={name}
            pattern="[A-Za-z0-9][A-Za-z0-9._\-]*"
            title="Letters, digits, dots, underscores and hyphens, starting with a letter or digit."
            onChange={(event) => {
              setName(event.target.value);
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>
      <Field label="Description">
        {(control) => (
          <input
            {...control}
            value={description}
            onChange={(event) => {
              setDescription(event.target.value);
            }}
            className={INPUT_CLASS}
          />
        )}
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Engine">
          {(control) => (
            <select
              {...control}
              value={engine}
              onChange={(event) => {
                setEngine(event.target.value as Engine);
              }}
              className={INPUT_CLASS}
            >
              {ENGINES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Engine version">
          {(control) => (
            <input
              {...control}
              required
              value={engineVersion}
              onChange={(event) => {
                setEngineVersion(event.target.value);
              }}
              className={`${INPUT_CLASS} font-mono`}
            />
          )}
        </Field>
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
