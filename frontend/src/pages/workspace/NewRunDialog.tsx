/** The dialog that starts a run: its type, its configuration and a reason. */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { invalidateQueries } from '@webbpulse/api-client/react';

import {
  RUN_ROLE_MISSING_MESSAGE,
  api,
  isRunRoleMissing,
  type ConfigVersion,
  type Workspace,
} from '../../api';
import {
  Button,
  Dialog,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  formatRelative,
  runPath,
} from '../../components';

/** Props for {@link NewRunDialog}. */
export interface NewRunDialogProps {
  open: boolean;
  onClose: () => void;
  workspace: Workspace;
  versions: readonly ConfigVersion[];
  /** The refetch key the workspace's run list is registered under. */
  runsKey: string;
}

/** The run types offered, in the order the hosted product lists them. */
const RUN_TYPES: readonly { id: 'apply' | 'plan'; label: string }[] = [
  { id: 'apply', label: 'Plan and apply (standard)' },
  { id: 'plan', label: 'Plan only' },
];

/** The new run dialog. Renders nothing while closed. */
export function NewRunDialog({
  open,
  onClose,
  workspace,
  versions,
  runsKey,
}: NewRunDialogProps): React.ReactElement | null {
  if (!open) {
    return null;
  }
  return (
    <Dialog
      open
      onClose={onClose}
      title="Start a new run"
      description="Choose how the run should proceed and which configuration it plans."
    >
      <NewRunForm
        workspace={workspace}
        versions={versions}
        runsKey={runsKey}
        onCancel={onClose}
      />
    </Dialog>
  );
}

/** The form inside the dialog. Mounted fresh each time the dialog opens. */
function NewRunForm({
  workspace,
  versions,
  runsKey,
  onCancel,
}: {
  workspace: Workspace;
  versions: readonly ConfigVersion[];
  runsKey: string;
  onCancel: () => void;
}): React.ReactElement {
  const navigate = useNavigate();
  const uploaded = versions
    .filter((version) => version.status === 'uploaded')
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
  const [runType, setRunType] = useState<'apply' | 'plan'>('apply');
  const [configVersionId, setConfigVersionId] = useState(
    uploaded[0]?.config_version_id ?? ''
  );
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (): Promise<void> => {
    if (configVersionId === '') {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({
        workspace_id: workspace.workspace_id,
        config_version_id: configVersionId,
        plan_only: runType === 'plan',
        message: message.trim(),
      });
      invalidateQueries(runsKey);
      onCancel();
      void navigate(
        runPath({ run_id: run.run_id, workspace_id: workspace.workspace_id })
      );
    } catch (thrown) {
      setError(
        isRunRoleMissing(thrown) ? new Error(RUN_ROLE_MISSING_MESSAGE) : thrown
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      aria-label="Start a new run"
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field label="Run type">
        {(control) => (
          <select
            {...control}
            autoFocus
            value={runType}
            onChange={(event) => {
              setRunType(event.target.value as 'apply' | 'plan');
            }}
            className={INPUT_CLASS}
          >
            {RUN_TYPES.map((type) => (
              <option key={type.id} value={type.id}>
                {type.label}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field
        label="Configuration version"
        hint={
          uploaded.length === 0
            ? 'Upload a configuration version before starting a run.'
            : 'The newest upload is selected.'
        }
      >
        {(control) => (
          <select
            {...control}
            required
            value={configVersionId}
            disabled={uploaded.length === 0}
            onChange={(event) => {
              setConfigVersionId(event.target.value);
            }}
            className={`${INPUT_CLASS} font-mono`}
          >
            {uploaded.map((version) => (
              <option
                key={version.config_version_id}
                value={version.config_version_id}
              >
                {version.config_version_id} (
                {formatRelative(version.created_at)})
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="Reason for run" hint="Optional. Shown as the run's title.">
        {(control) => (
          <textarea
            {...control}
            rows={2}
            maxLength={1024}
            value={message}
            onChange={(event) => {
              setMessage(event.target.value);
            }}
            className={`${INPUT_CLASS} h-auto py-1.5`}
          />
        )}
      </Field>
      <ErrorNotice error={error} />
      <div className="flex justify-end gap-2 pt-1">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="primary"
          busy={busy}
          busyLabel="Starting the run"
          disabled={configVersionId === ''}
        >
          Start run
        </Button>
      </div>
    </form>
  );
}
