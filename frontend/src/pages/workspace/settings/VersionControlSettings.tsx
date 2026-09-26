/** The version control settings page: the repository a workspace runs from and when. */

import { useEffect, useState } from 'react';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type Workspace, type WorkspaceUpdate } from '../../../api';
import { Button, Dialog, ErrorNotice } from '../../../components';
import { useWorkspace } from '../../workspaceContext';
import { RepositoryPicker } from '../vcs/RepositoryPicker';
import type { InstalledRepository } from '../vcs/useInstalledRepositories';
import { VcsFields } from '../vcs/VcsFields';
import {
  sameRepository,
  vcsSettingsOf,
  versionControlBody,
  type VcsSettings,
} from '../vcs/vcsSettings';

/** The version control settings page. */
export function VersionControlSettings(): React.ReactElement {
  const { workspace, keys } = useWorkspace();
  return (
    <div className="max-w-2xl space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-text-strong">
          Version control
        </h2>
        <p className="mt-1 text-sm text-text-muted">
          Connect a GitHub repository and pushes to its branch start runs, while
          pull requests get plan-only runs.
        </p>
      </div>
      <VersionControlForm workspace={workspace} queryKey={keys.workspace} />
    </div>
  );
}

/** The connect, change and disconnect form. */
function VersionControlForm({
  workspace,
  queryKey,
}: {
  workspace: Workspace;
  queryKey: string;
}): React.ReactElement {
  const connected = workspace.vcs_repo ?? null;
  const [repository, setRepository] = useState<string | null>(connected);
  const [defaultBranch, setDefaultBranch] = useState<string | null>(null);
  const [choosing, setChoosing] = useState(connected === null);
  const [settings, setSettings] = useState<VcsSettings>(
    vcsSettingsOf(workspace)
  );
  const [confirming, setConfirming] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    setRepository(workspace.vcs_repo ?? null);
    setChoosing((workspace.vcs_repo ?? null) === null);
    setSettings(vcsSettingsOf(workspace));
  }, [workspace]);

  const save = useMutationWithRefetch(
    (body: WorkspaceUpdate) =>
      api.updateWorkspace(workspace.workspace_id, body),
    queryKey
  );

  const pick = (picked: InstalledRepository): void => {
    setRepository(picked.full_name);
    setDefaultBranch(picked.default_branch ?? null);
    setSaved(null);
    setSettings((current) => ({ ...current, branch: '' }));
  };

  const submit = async (): Promise<void> => {
    if (repository === null) {
      return;
    }
    setSaved(null);
    try {
      await save.mutate(versionControlBody(workspace, repository, settings));
      setSaved(connected === null ? 'Connected.' : 'Saved.');
    } catch {
      return;
    }
  };

  const disconnect = async (): Promise<void> => {
    setSaved(null);
    try {
      await save.mutate({
        vcs_repo: null,
        tracked_branch: null,
        trigger_patterns: null,
      });
      setConfirming(false);
      setDefaultBranch(null);
      setSaved('Disconnected.');
    } catch {
      setConfirming(false);
    }
  };

  return (
    <>
      <form
        aria-label="Version control settings"
        className="space-y-4 rounded-lg border border-line bg-panel p-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-2 text-sm">
          {connected !== null && !choosing ? null : (
            <span className="text-text-muted">Repository</span>
          )}
          {connected !== null && !choosing ? (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line bg-raised px-3 py-2">
              <dl
                data-testid="connected-repository"
                className="grid min-w-0 grid-cols-[auto_1fr] items-baseline gap-x-3 gap-y-1"
              >
                <dt className="text-xs text-text-faint">Repository</dt>
                <dd className="min-w-0 truncate">
                  <a
                    href={`https://github.com/${connected}`}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-text-strong hover:text-accent hover:underline"
                  >
                    {connected}
                  </a>
                </dd>
                {workspace.tracked_branch ? (
                  <>
                    <dt className="text-xs text-text-faint">Branch</dt>
                    <dd className="font-mono text-xs text-text">
                      {workspace.tracked_branch}
                    </dd>
                  </>
                ) : null}
              </dl>
              <span className="flex gap-2">
                <Button
                  variant="secondary"
                  onClick={() => {
                    setChoosing(true);
                  }}
                >
                  Change repository
                </Button>
                <Button
                  variant="danger"
                  onClick={() => {
                    setConfirming(true);
                  }}
                >
                  Disconnect
                </Button>
              </span>
            </div>
          ) : (
            <RepositoryPicker value={repository} onChange={pick} />
          )}
          {choosing && repository !== null ? (
            <p className="text-xs text-text-muted">
              Selected <span className="font-mono">{repository}</span>.
            </p>
          ) : null}
        </div>
        {repository !== null ? (
          <>
            <VcsFields
              value={settings}
              onChange={(next) => {
                setSettings(next);
                setSaved(null);
              }}
              defaultBranch={defaultBranch}
              currentBranch={
                sameRepository(repository, connected)
                  ? (workspace.tracked_branch ?? null)
                  : null
              }
            />
            <ErrorNotice error={save.error} />
            <div className="flex items-center gap-3">
              <Button
                type="submit"
                variant="primary"
                busy={save.isMutating}
                busyLabel="Saving the version control settings"
              >
                {connected === null
                  ? 'Connect repository'
                  : 'Update VCS settings'}
              </Button>
              {choosing && connected !== null ? (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setChoosing(false);
                    setRepository(connected);
                    setSettings(vcsSettingsOf(workspace));
                  }}
                >
                  Cancel
                </Button>
              ) : null}
              {saved !== null ? (
                <span role="status" className="text-sm text-success">
                  {saved}
                </span>
              ) : null}
            </div>
          </>
        ) : saved !== null ? (
          <span role="status" className="text-sm text-success">
            {saved}
          </span>
        ) : null}
      </form>
      <Dialog
        open={confirming}
        onClose={() => {
          setConfirming(false);
        }}
        title="Disconnect the repository"
        description="Pushes and pull requests stop starting runs. The workspace, its state and its runs stay as they are."
      >
        <div className="flex justify-end gap-2 pt-1">
          <Button
            variant="ghost"
            onClick={() => {
              setConfirming(false);
            }}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            busy={save.isMutating}
            busyLabel="Disconnecting the repository"
            onClick={() => {
              void disconnect();
            }}
          >
            Disconnect repository
          </Button>
        </div>
      </Dialog>
    </>
  );
}
