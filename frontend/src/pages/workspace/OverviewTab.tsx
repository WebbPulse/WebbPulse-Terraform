/** The overview tab: the workspace's settings, editable in place. */

import { useEffect, useState } from 'react';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import {
  RUN_ROLE_ARN_MESSAGE,
  api,
  isRunRoleArn,
  type Engine,
  type Workspace,
} from '../../api';
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
  const [description, setDescription] = useState(workspace.description);
  const [engine, setEngine] = useState<Engine>(workspace.engine);
  const [engineVersion, setEngineVersion] = useState(workspace.engine_version);
  const [workingDirectory, setWorkingDirectory] = useState(
    workspace.working_directory
  );
  const [runRoleArn, setRunRoleArn] = useState(workspace.run_role_arn);
  const [invalidArn, setInvalidArn] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setDescription(workspace.description);
    setEngine(workspace.engine);
    setEngineVersion(workspace.engine_version);
    setWorkingDirectory(workspace.working_directory);
    setRunRoleArn(workspace.run_role_arn);
    setInvalidArn(false);
  }, [workspace]);

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.updateWorkspace(workspace.workspace_id, {
        description,
        engine,
        engine_version: engineVersion,
        working_directory: workingDirectory,
        run_role_arn: runRoleArn.trim(),
      }),
    queryKey
  );

  const submit = async (): Promise<void> => {
    setSaved(false);
    if (!isRunRoleArn(runRoleArn)) {
      setInvalidArn(true);
      return;
    }
    setInvalidArn(false);
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
      <label className="block text-sm">
        <span className="text-surface-300">Run role ARN</span>
        <input
          required
          value={runRoleArn}
          aria-invalid={invalidArn}
          aria-describedby={invalidArn ? 'overview-run-role-error' : undefined}
          placeholder="arn:aws:iam::123456789012:role/terraform-run"
          onChange={(event) => {
            setRunRoleArn(event.target.value);
            setInvalidArn(false);
          }}
          className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2 font-mono"
        />
      </label>
      {invalidArn ? (
        <p
          id="overview-run-role-error"
          role="alert"
          className="text-sm text-rose-300"
        >
          {RUN_ROLE_ARN_MESSAGE}
        </p>
      ) : null}
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
