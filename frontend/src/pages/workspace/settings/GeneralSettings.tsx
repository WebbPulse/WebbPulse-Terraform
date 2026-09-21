/** The general settings page: the editable fields of the workspace. */

import { useEffect, useState } from 'react';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type Engine, type Workspace } from '../../../api';
import {
  Button,
  CopyButton,
  ErrorNotice,
  Field,
  INPUT_CLASS,
} from '../../../components';
import { useWorkspace } from '../../workspaceContext';

/** The engines a workspace can run. */
const ENGINES: readonly Engine[] = ['terraform', 'tofu'];

/** The engine to edit, defaulted the way the backend defaults an absent one. */
function engineOf(workspace: Workspace): Engine {
  return workspace.engine ?? 'terraform';
}

/** The general settings page. */
export function GeneralSettings(): React.ReactElement {
  const { workspace, keys } = useWorkspace();
  return (
    <div className="max-w-2xl space-y-4">
      <h2 className="text-sm font-semibold text-text-strong">
        General settings
      </h2>
      <SettingsForm workspace={workspace} queryKey={keys.workspace} />
    </div>
  );
}

/** The workspace settings form. The run role lives with the AWS account page. */
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
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="text-sm">
        <span className="text-text-muted">ID</span>
        <span className="mt-1 flex items-center gap-2 font-mono text-xs text-text">
          {workspace.workspace_id}
          <CopyButton
            value={workspace.workspace_id}
            subject="the workspace id"
            className="h-6 px-1.5"
          />
        </span>
      </div>
      <div className="text-sm">
        <span className="text-text-muted">Name</span>
        <span className="mt-1 block font-mono text-text">{workspace.name}</span>
      </div>
      <Field label="Description" hint="Optional.">
        {(control) => (
          <textarea
            {...control}
            rows={2}
            value={description}
            onChange={(event) => {
              setDescription(event.target.value);
            }}
            className={`${INPUT_CLASS} h-auto py-1.5`}
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
      <Field
        label="Working directory"
        hint="Relative to the root of the uploaded archive."
      >
        {(control) => (
          <input
            {...control}
            value={workingDirectory}
            placeholder="."
            onChange={(event) => {
              setWorkingDirectory(event.target.value);
            }}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>
      <ErrorNotice error={error} />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Saving the workspace"
        >
          Save settings
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
