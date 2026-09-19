/** The overview tab: the AWS connection and the settings, editable in place. */

import { useEffect, useState } from 'react';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type Engine, type Workspace } from '../../api';
import { Button, ErrorNotice } from '../../components';
import { ConnectAccountPanel } from './ConnectAccountPanel';

/** Props for {@link OverviewTab}. */
export interface OverviewTabProps {
  workspace: Workspace;
  /** The refetch key the workspace read is registered under. */
  queryKey: string;
  /** Whether the checklist above the tabs is gone, so this tab owns the connection. */
  setupComplete: boolean;
}

/** The engines a workspace can run. */
const ENGINES: readonly Engine[] = ['terraform', 'tofu'];

/** The engine to edit, defaulted the way the backend defaults an absent one. */
function engineOf(workspace: Workspace): Engine {
  return workspace.engine ?? 'terraform';
}

/** The connection section and the settings form. */
export function OverviewTab({
  workspace,
  queryKey,
  setupComplete,
}: OverviewTabProps): React.ReactElement {
  return (
    <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section aria-labelledby="overview-settings" className="space-y-3">
        <h2
          id="overview-settings"
          className="text-sm font-semibold text-surface-50"
        >
          Settings
        </h2>
        <SettingsForm workspace={workspace} queryKey={queryKey} />
      </section>
      <section aria-labelledby="overview-account" className="space-y-3">
        <h2
          id="overview-account"
          className="text-sm font-semibold text-surface-50"
        >
          AWS account
        </h2>
        {setupComplete ? (
          <ConnectAccountPanel
            workspace={workspace}
            queryKey={queryKey}
            collapsible
          />
        ) : (
          <p className="text-sm text-surface-300">
            The setup checklist above walks through connecting an account. The
            connection settings move here once the first plan has run.
          </p>
        )}
      </section>
    </div>
  );
}

/** The workspace settings form. The run role lives with the connection. */
function SettingsForm({
  workspace,
  queryKey,
}: {
  workspace: Workspace;
  queryKey: string;
}): React.ReactElement {
  const [description, setDescription] = useState(workspace.description ?? '');
  const [engine, setEngine] = useState<Engine>(engineOf(workspace));
  const [engineVersion, setEngineVersion] = useState(workspace.engine_version);
  const [workingDirectory, setWorkingDirectory] = useState(
    workspace.working_directory ?? ''
  );
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setDescription(workspace.description ?? '');
    setEngine(engineOf(workspace));
    setEngineVersion(workspace.engine_version);
    setWorkingDirectory(workspace.working_directory ?? '');
  }, [workspace]);

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.updateWorkspace(workspace.workspace_id, {
        description,
        engine,
        engine_version: engineVersion,
        working_directory: workingDirectory,
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
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <p className="text-sm">
        <span className="text-surface-300">Name</span>
        <span className="mt-1 block font-mono text-surface-100">
          {workspace.name}
        </span>
      </p>
      <label className="block text-sm">
        <span className="text-surface-300">Description</span>
        <textarea
          rows={2}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
          className={`${INPUT} h-auto py-1.5`}
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
      <label className="block text-sm">
        <span className="text-surface-300">Working directory</span>
        <input
          value={workingDirectory}
          placeholder="."
          onChange={(event) => {
            setWorkingDirectory(event.target.value);
          }}
          className={`${INPUT} font-mono`}
        />
      </label>
      <ErrorNotice error={error} />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Saving the workspace"
        >
          Save changes
        </Button>
        {saved ? (
          <span role="status" className="text-sm text-emerald-300">
            Saved.
          </span>
        ) : null}
      </div>
    </form>
  );
}

/** The input styling the settings fields share. */
const INPUT =
  'mt-1 h-8 w-full rounded-md border border-surface-600 bg-surface-900 px-2.5 text-sm text-surface-100 focus-visible:border-brand-400 focus-visible:ring-1 focus-visible:ring-brand-400 focus-visible:outline-none';
