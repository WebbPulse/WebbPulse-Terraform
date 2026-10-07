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
  useIsAdmin,
} from '../../../components';
import { useWorkspace } from '../../workspaceContext';
import { EngineFields } from '../EngineFields';
import { engineVersionProblem } from '../engineVersions';
import { ProjectSettings } from './ProjectSettings';

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
      <ProjectSettings workspace={workspace} queryKey={keys.workspace} />
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
  const [engine, setEngine] = useState(() => ({
    engine: engineOf(workspace),
    version: workspace.engine_version,
  }));
  const [workingDirectory, setWorkingDirectory] = useState(
    workspace.working_directory ?? ''
  );
  const [autoApply, setAutoApply] = useState(workspace.auto_apply ?? false);
  const [saved, setSaved] = useState(false);
  const isAdmin = useIsAdmin();

  useEffect(() => {
    setAutoApply(workspace.auto_apply ?? false);
    setDescription(workspace.description ?? '');
    setEngine({
      engine: engineOf(workspace),
      version: workspace.engine_version,
    });
    setWorkingDirectory(workspace.working_directory ?? '');
  }, [workspace]);

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.updateWorkspace(workspace.workspace_id, {
        description,
        engine: engine.engine,
        engine_version: engine.version.trim().replace(/^v/, ''),
        working_directory: workingDirectory,
        ...(autoApply === (workspace.auto_apply ?? false)
          ? {}
          : { auto_apply: autoApply }),
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
      <EngineFields
        key={`${workspace.workspace_id}:${workspace.engine_version}`}
        value={engine}
        onChange={setEngine}
      />
      <Field
        label="Working directory"
        hint={
          workspace.vcs_repo
            ? 'Relative to the root of the repository.'
            : 'Relative to the root of the uploaded archive.'
        }
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
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium text-text-strong">
          Auto-apply
        </legend>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            aria-label="Auto-apply API, CLI and VCS runs"
            checked={autoApply}
            disabled={!isAdmin}
            onChange={(event) => {
              setAutoApply(event.target.checked);
            }}
            className="mt-0.5 accent-accent"
          />
          <span>
            <span className="text-text">Auto-apply API, CLI and VCS runs</span>
            <span className="block text-xs text-text-muted">
              A successful plan with changes applies without a confirmation.
              Plan-only and pull request runs never apply.
              {isAdmin ? '' : ' Only an admin can change this.'}
            </span>
          </span>
        </label>
      </fieldset>
      <ErrorNotice error={error} />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Saving the workspace"
          disabled={engineVersionProblem(engine.version) !== null}
        >
          Save settings
        </Button>
        {saved ? (
          <span role="status" className="text-sm text-success">
            Saved.
          </span>
        ) : null}
      </div>
    </form>
  );
}
