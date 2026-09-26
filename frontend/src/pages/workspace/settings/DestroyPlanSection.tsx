/** The destroy plan section of the destruction and deletion page. */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { invalidateQueries } from '@webbpulse/api-client/react';

import { RUN_ROLE_MISSING_MESSAGE, api, isRunRoleMissing } from '../../../api';
import {
  Button,
  Dialog,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  runPath,
} from '../../../components';
import { useWorkspace } from '../../workspaceContext';
import { latestUploaded } from './latestUploaded';

/** Queues a destroy plan against the workspace's newest configuration. */
export function DestroyPlanSection(): React.ReactElement {
  const { workspace, versions } = useWorkspace();
  const [confirming, setConfirming] = useState(false);
  const latest = latestUploaded(versions);
  return (
    <section className="space-y-3 rounded-lg border border-danger-line bg-panel p-4">
      <h3 className="text-sm font-medium text-text-strong">
        Destroy infrastructure
      </h3>
      <p className="max-w-prose text-sm text-text-muted">
        Queues a destroy plan that removes every resource this workspace
        manages. The plan waits for confirmation before anything is destroyed,
        like any other run.
      </p>
      {latest === null ? (
        <p className="text-xs text-text-faint">
          Upload a configuration version before queueing a destroy plan.
        </p>
      ) : null}
      <Button
        variant="danger"
        disabled={latest === null}
        onClick={() => {
          setConfirming(true);
        }}
      >
        Queue destroy plan
      </Button>
      {confirming && latest !== null ? (
        <Dialog
          open
          onClose={() => {
            setConfirming(false);
          }}
          title="Queue destroy plan"
          description="The plan destroys every resource this workspace manages once confirmed."
        >
          <DestroyForm
            name={workspace.name}
            workspaceId={workspace.workspace_id}
            configVersionId={latest.config_version_id}
            onCancel={() => {
              setConfirming(false);
            }}
          />
        </Dialog>
      ) : null}
    </section>
  );
}

/** The confirmation form: the name typed back, then the destroy run. */
function DestroyForm({
  name,
  workspaceId,
  configVersionId,
  onCancel,
}: {
  name: string;
  workspaceId: string;
  configVersionId: string;
  onCancel: () => void;
}): React.ReactElement {
  const navigate = useNavigate();
  const { keys } = useWorkspace();
  const [typed, setTyped] = useState('');
  const [planOnly, setPlanOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (): Promise<void> => {
    if (typed !== name) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({
        workspace_id: workspaceId,
        config_version_id: configVersionId,
        plan_only: planOnly,
        is_destroy: true,
        message: 'Destroy plan queued from settings',
      });
      invalidateQueries([keys.runs]);
      void navigate(runPath({ run_id: run.run_id, workspace_id: workspaceId }));
    } catch (thrown) {
      setError(
        isRunRoleMissing(thrown) ? new Error(RUN_ROLE_MISSING_MESSAGE) : thrown
      );
      setBusy(false);
    }
  };

  return (
    <form
      aria-label="Queue a destroy plan"
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field
        label={`Type ${name} to confirm`}
        hint={`Plans against configuration version ${configVersionId}.`}
      >
        {(control) => (
          <input
            {...control}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>
      <label className="flex items-center gap-2 text-sm text-text-muted">
        <input
          type="checkbox"
          checked={planOnly}
          onChange={(event) => {
            setPlanOnly(event.target.checked);
          }}
          className="size-3.5 accent-accent"
        />
        Plan only, never apply
      </label>
      <ErrorNotice error={error} />
      <div className="flex justify-end gap-2 pt-1">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="danger"
          busy={busy}
          busyLabel="Queueing the destroy plan"
          disabled={typed !== name}
        >
          Queue destroy plan
        </Button>
      </div>
    </form>
  );
}
