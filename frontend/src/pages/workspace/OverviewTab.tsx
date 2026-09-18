/** The overview tab: the workspace's settings, editable in place. */

import { useEffect, useState } from 'react';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type Engine, type Workspace } from '../../api';
import { ErrorNotice, Spinner } from '../../components';

/** Props for {@link OverviewTab}. */
export interface OverviewTabProps {
  workspace: Workspace;
  /** The refetch key the workspace read is registered under. */
  queryKey: string;
}

/** The engines a workspace can run. */
const ENGINES: readonly Engine[] = ['terraform', 'tofu'];

/** The workspace settings form. */
export function OverviewTab({
  workspace,
  queryKey,
}: OverviewTabProps): React.ReactElement {
  const [name, setName] = useState(workspace.name);
  const [description, setDescription] = useState(workspace.description ?? '');
  const [engine, setEngine] = useState<Engine>(workspace.engine);
  const [engineVersion, setEngineVersion] = useState(workspace.engine_version);
  const [workingDirectory, setWorkingDirectory] = useState(
    workspace.working_directory ?? ''
  );
  const [autoApply, setAutoApply] = useState(workspace.auto_apply);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setName(workspace.name);
    setDescription(workspace.description ?? '');
    setEngine(workspace.engine);
    setEngineVersion(workspace.engine_version);
    setWorkingDirectory(workspace.working_directory ?? '');
    setAutoApply(workspace.auto_apply);
  }, [workspace]);

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.updateWorkspace(workspace.workspace_id, {
        name,
        description,
        engine,
        engine_version: engineVersion,
        working_directory: workingDirectory,
        auto_apply: autoApply,
      }),
    queryKey
  );

  const submit = async (): Promise<void> => {
    setSaved(false);
    try {
      await mutate();
      setSaved(true);
    } catch {
      return;
    }
  };

  return (
    <form
      aria-label="Workspace settings"
      className="max-w-lg space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="block text-sm">
        <span className="text-surface-300">Name</span>
        <input
          required
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
          className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2"
        />
      </label>
      <label className="block text-sm">
        <span className="text-surface-300">Description</span>
        <textarea
          rows={2}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
          className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2"
        />
      </label>
      <label className="block text-sm">
        <span className="text-surface-300">Engine</span>
        <select
          value={engine}
          onChange={(event) => {
            setEngine(event.target.value as Engine);
          }}
          className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2"
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
          className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2"
        />
      </label>
      <label className="block text-sm">
        <span className="text-surface-300">Working directory</span>
        <input
          value={workingDirectory}
          onChange={(event) => {
            setWorkingDirectory(event.target.value);
          }}
          className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2 font-mono"
        />
      </label>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={autoApply}
          onChange={(event) => {
            setAutoApply(event.target.checked);
          }}
        />
        <span className="text-surface-300">Apply without confirmation</span>
      </label>
      <ErrorNotice error={error} />
      {saved ? (
        <p role="status" className="text-sm text-emerald-300">
          Saved.
        </p>
      ) : null}
      <button
        type="submit"
        disabled={isMutating}
        className="flex items-center gap-2 rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {isMutating ? <Spinner label="Saving the workspace" /> : null}
        Save changes
      </button>
    </form>
  );
}
