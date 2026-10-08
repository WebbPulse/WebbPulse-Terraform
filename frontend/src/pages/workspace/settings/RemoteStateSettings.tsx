/** Decides which other workspaces' runs may read this workspace's non-sensitive outputs. */

import { useEffect, useMemo, useState } from 'react';
import {
  usePolledQuery,
  useMutationWithRefetch,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type Workspace, type WorkspaceList } from '../../../api';
import { Button, ErrorNotice, INPUT_CLASS } from '../../../components';
import { WORKSPACES_KEY } from '../../Workspaces';

/** The consumers a workspace names, sorted so two lists compare by value. */
function consumersOf(workspace: Workspace): string[] {
  return [...(workspace.remote_state_consumer_ids ?? [])].sort();
}

/** Whether two sorted id lists hold the same ids. */
function sameIds(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

/** The remote state sharing form on the general settings page. */
export function RemoteStateSettings({
  workspace,
  queryKey,
}: {
  workspace: Workspace;
  queryKey: string;
}): React.ReactElement {
  const auth = useQueryAuth();
  const storedGlobal = workspace.global_remote_state ?? false;
  const storedConsumers = useMemo(() => consumersOf(workspace), [workspace]);
  const [shareAll, setShareAll] = useState(storedGlobal);
  const [consumers, setConsumers] = useState<string[]>(storedConsumers);
  const [filter, setFilter] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setShareAll(storedGlobal);
    setConsumers(storedConsumers);
  }, [storedGlobal, storedConsumers]);

  const listing = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }, { sort: 'name' }),
    { intervalMs: 60_000, queryKey: [WORKSPACES_KEY, 'name'], auth }
  );
  const others = (listing.data?.items ?? []).filter(
    (item) => item.workspace_id !== workspace.workspace_id
  );
  const needle = filter.trim().toLowerCase();
  const shown =
    needle === ''
      ? others
      : others.filter((item) => item.name.toLowerCase().includes(needle));
  const names = new Map(others.map((item) => [item.workspace_id, item.name]));

  const { mutate, isMutating, error } = useMutationWithRefetch(
    () =>
      api.updateWorkspace(workspace.workspace_id, {
        global_remote_state: shareAll,
        remote_state_consumer_ids: consumers,
      }),
    queryKey
  );

  const changed =
    shareAll !== storedGlobal || !sameIds(consumers, storedConsumers);

  const toggle = (workspaceId: string, on: boolean): void => {
    setSaved(false);
    setConsumers((current) =>
      on
        ? [...current, workspaceId].sort()
        : current.filter((value) => value !== workspaceId)
    );
  };

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
      aria-label="Remote state sharing"
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div>
        <h3 className="text-sm font-medium text-text-strong">
          Remote state sharing
        </h3>
        <p className="mt-1 text-xs text-text-muted">
          Choose which workspaces&apos; runs may read this workspace&apos;s
          outputs with a{' '}
          <code className="font-mono">terraform_remote_state</code> data
          source. Sensitive outputs are never shared.
        </p>
      </div>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          aria-label="Share with all workspaces in this organization"
          checked={shareAll}
          onChange={(event) => {
            setSaved(false);
            setShareAll(event.target.checked);
          }}
          className="mt-0.5 accent-accent"
        />
        <span>
          <span className="text-text">
            Share with all workspaces in this organization
          </span>
          <span className="block text-xs text-text-muted">
            Every workspace&apos;s runs can read these outputs, including
            workspaces created later.
          </span>
        </span>
      </label>
      {shareAll ? null : (
        <fieldset className="space-y-2">
          <legend className="text-sm text-text-muted">
            Share with specific workspaces
          </legend>
          {consumers.length === 0 ? (
            <p className="text-xs text-text-faint">
              No workspaces can read this workspace&apos;s outputs.
            </p>
          ) : (
            <ul
              aria-label="Selected workspaces"
              className="flex flex-wrap gap-1.5"
            >
              {consumers.map((workspaceId) => (
                <li
                  key={workspaceId}
                  className="flex items-center gap-1 rounded-md border border-line-strong bg-raised px-2 py-0.5 font-mono text-xs text-text"
                >
                  {names.get(workspaceId) ?? workspaceId}
                  <button
                    type="button"
                    aria-label={`Stop sharing with ${names.get(workspaceId) ?? workspaceId}`}
                    onClick={() => {
                      toggle(workspaceId, false);
                    }}
                    className="text-text-muted hover:text-text-strong"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
          {others.length === 0 ? (
            <p className="text-xs text-text-faint">
              {listing.data
                ? 'There are no other workspaces yet.'
                : 'Loading workspaces.'}
            </p>
          ) : (
            <>
              <input
                type="search"
                aria-label="Filter workspaces"
                placeholder="Filter workspaces"
                value={filter}
                onChange={(event) => {
                  setFilter(event.target.value);
                }}
                className={INPUT_CLASS}
              />
              <ul
                aria-label="Workspaces"
                className="max-h-56 overflow-y-auto rounded-md border border-line"
              >
                {shown.map((item) => (
                  <li key={item.workspace_id}>
                    <label className="flex items-center gap-2 px-2.5 py-1.5 text-sm hover:bg-raised">
                      <input
                        type="checkbox"
                        checked={consumers.includes(item.workspace_id)}
                        onChange={(event) => {
                          toggle(item.workspace_id, event.target.checked);
                        }}
                        className="accent-accent"
                      />
                      <span className="font-mono text-text">{item.name}</span>
                    </label>
                  </li>
                ))}
                {shown.length === 0 ? (
                  <li className="px-2.5 py-1.5 text-xs text-text-faint">
                    No workspace matches.
                  </li>
                ) : null}
              </ul>
            </>
          )}
        </fieldset>
      )}
      <ErrorNotice error={error} />
      <div className="flex items-center gap-3">
        <Button
          type="submit"
          variant="primary"
          busy={isMutating}
          busyLabel="Saving remote state sharing"
          disabled={!changed}
        >
          Save remote state sharing
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
