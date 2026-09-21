/** The destruction and deletion page: deleting the workspace record. */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { invalidateQueries } from '@webbpulse/api-client/react';

import { api } from '../../../api';
import {
  Button,
  Dialog,
  ErrorNotice,
  Field,
  INPUT_CLASS,
} from '../../../components';
import { useWorkspace } from '../../workspaceContext';
import { WORKSPACES_KEY } from '../../Workspaces';

/** The destruction and deletion page. */
export function DeletionSettings(): React.ReactElement {
  const { workspace } = useWorkspace();
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="max-w-2xl space-y-4">
      <h2 className="text-sm font-semibold text-text-strong">
        Destruction and deletion
      </h2>
      <section className="space-y-3 rounded-lg border border-danger-line bg-panel p-4">
        <h3 className="text-sm font-medium text-text-strong">
          Delete workspace
        </h3>
        <p className="max-w-prose text-sm text-text-muted">
          Deleting the workspace removes it and its variables. Resources the
          runs created in your AWS account are not destroyed first. Delete the
          workspace only once nothing it manages is still in use.
        </p>
        <Button
          variant="danger"
          onClick={() => {
            setConfirming(true);
          }}
        >
          Delete workspace
        </Button>
      </section>
      <Dialog
        open={confirming}
        onClose={() => {
          setConfirming(false);
        }}
        title="Delete workspace"
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

/** The confirmation form: the name typed back, then the delete. */
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (): Promise<void> => {
    if (typed !== name) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.deleteWorkspace(workspaceId);
      invalidateQueries([WORKSPACES_KEY]);
      void navigate('/workspaces', { replace: true });
    } catch (thrown) {
      setError(thrown);
      setBusy(false);
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
            value={typed}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
            className={`${INPUT_CLASS} font-mono`}
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
          variant="danger"
          busy={busy}
          busyLabel="Deleting the workspace"
          disabled={typed !== name}
        >
          Delete workspace
        </Button>
      </div>
    </form>
  );
}
