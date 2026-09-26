/** The destruction and deletion page: a destroy plan, then deleting the workspace record. */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { invalidateQueries } from '@webbpulse/api-client/react';

import { api, isWorkspaceManagesResources } from '../../../api';
import {
  Button,
  Dialog,
  ErrorNotice,
  Field,
  INPUT_CLASS,
} from '../../../components';
import { useWorkspace } from '../../workspaceContext';
import { WORKSPACES_KEY } from '../../Workspaces';
import { DestroyPlanSection } from './DestroyPlanSection';

/** The destruction and deletion page. */
export function DeletionSettings(): React.ReactElement {
  const { workspace } = useWorkspace();
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="max-w-2xl space-y-4">
      <h2 className="text-sm font-semibold text-text-strong">
        Destruction and deletion
      </h2>
      <DestroyPlanSection />
      <section className="space-y-3 rounded-lg border border-danger-line bg-panel p-4">
        <h3 className="text-sm font-medium text-text-strong">
          Delete from WebbPulse
        </h3>
        <p className="max-w-prose text-sm text-text-muted">
          Deleting the workspace removes it, its variables, its finished runs
          and its current state from WebbPulse. A workspace whose state still
          tracks resources cannot be deleted until a destroy plan above has been
          applied, unless you force the delete and leave those resources running
          unmanaged in your AWS account.
        </p>
        <Button
          variant="danger"
          onClick={() => {
            setConfirming(true);
          }}
        >
          Delete from WebbPulse
        </Button>
      </section>
      <Dialog
        open={confirming}
        onClose={() => {
          setConfirming(false);
        }}
        title="Delete from WebbPulse"
        description="This cannot be undone."
      >
        <DeleteForm
          name={workspace.name}
          workspaceId={workspace.workspace_id}
          onCancel={() => {
            setConfirming(false);
          }}
        />
      </Dialog>
    </div>
  );
}

/** The phrase a force delete has to be typed back with, on top of the name. */
const FORCE_DELETE_PHRASE = 'force delete';

/**
 * The confirmation form: the name typed back, then a safe delete. When the API
 * refuses because state still tracks resources, the form turns into a force
 * delete held behind a second typed confirmation.
 */
function DeleteForm({
  name,
  workspaceId,
  onCancel,
}: {
  name: string;
  workspaceId: string;
  onCancel: () => void;
}): React.ReactElement {
  const navigate = useNavigate();
  const [typed, setTyped] = useState('');
  const [forcing, setForcing] = useState(false);
  const [typedForce, setTypedForce] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const ready = forcing
    ? typed === name && typedForce === FORCE_DELETE_PHRASE
    : typed === name;

  const submit = async (): Promise<void> => {
    if (!ready) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.deleteWorkspace(workspaceId, { force: forcing });
      invalidateQueries(WORKSPACES_KEY);
      void navigate('/workspaces', { replace: true });
    } catch (thrown) {
      setBusy(false);
      if (!forcing && isWorkspaceManagesResources(thrown)) {
        setForcing(true);
        return;
      }
      setError(thrown);
    }
  };

  return (
    <form
      aria-label="Delete the workspace"
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <Field label={`Type ${name} to confirm`}>
        {(control) => (
          <input
            {...control}
            autoFocus
            autoComplete="off"
            spellCheck={false}
            readOnly={forcing}
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className={`${INPUT_CLASS} font-mono`}
          />
        )}
      </Field>
      {forcing ? (
        <div className="space-y-3" data-testid="force-delete">
          <div
            role="alert"
            className="space-y-1 rounded-md border border-danger-line bg-danger-soft p-3 text-sm"
          >
            <p className="font-medium text-text-strong">
              This workspace still manages resources.
            </p>
            <p className="text-text-muted">
              Its state still tracks infrastructure, so a safe delete was
              refused. Queue a destroy plan from Destroy infrastructure above
              and apply it first. A force delete removes the workspace anyway
              and leaves those resources running, no longer managed by
              WebbPulse.
            </p>
          </div>
          <Field label={`Type ${FORCE_DELETE_PHRASE} to force the delete`}>
            {(control) => (
              <input
                {...control}
                autoFocus
                autoComplete="off"
                spellCheck={false}
                value={typedForce}
                onChange={(event) => {
                  setTypedForce(event.target.value);
                }}
                className={`${INPUT_CLASS} font-mono`}
              />
            )}
          </Field>
        </div>
      ) : null}
      <ErrorNotice error={error} />
      <div className="flex justify-end gap-2 pt-1">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          type="submit"
          variant="danger"
          busy={busy}
          busyLabel="Deleting the workspace"
          disabled={!ready}
        >
          {forcing ? 'Force delete' : 'Delete from WebbPulse'}
        </Button>
      </div>
    </form>
  );
}
