/** The configuration versions tab: upload a tarball, then start a run from it. */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { invalidateQueries } from '@webbpulse/api-client/react';

import {
  RUN_ROLE_MISSING_MESSAGE,
  api,
  isConnected,
  isRunRoleMissing,
  type ConfigVersion,
  type Workspace,
} from '../../api';
import {
  Button,
  ErrorNotice,
  Spinner,
  formatBytes,
  formatDateTime,
} from '../../components';
import { UploadConfigForm } from './UploadConfigForm';

/** Props for {@link ConfigVersionsTab}. The page owns the query and hands its state down. */
export interface ConfigVersionsTabProps {
  workspace: Workspace;
  versions: ConfigVersion[];
  isLoading: boolean;
  error: unknown;
  /** The refetch keys for the workspace, its versions and its runs. */
  keys: { workspace: string; versions: string; runs: string };
}

/** The configuration versions list and its upload form. */
export function ConfigVersionsTab({
  workspace,
  versions,
  isLoading,
  error,
  keys,
}: ConfigVersionsTabProps): React.ReactElement {
  const connected = isConnected(workspace);
  return (
    <div className="space-y-5">
      <ErrorNotice error={error} />
      <div className="rounded-lg border border-surface-700 bg-surface-800 p-4">
        <UploadConfigForm
          workspaceId={workspace.workspace_id}
          queryKey={keys.versions}
          label="Upload a configuration version"
          fileLabel="Configuration tarball"
        />
      </div>
      {connected ? null : (
        <p role="note" className="text-sm text-amber-300">
          {RUN_ROLE_MISSING_MESSAGE} Runs stay disabled until the connection
          check passes on the Overview tab.
        </p>
      )}
      {isLoading ? (
        <Spinner label="Loading configuration versions" />
      ) : (
        <ConfigVersionTable
          workspaceId={workspace.workspace_id}
          versions={versions}
          connected={connected}
          runsKey={keys.runs}
        />
      )}
    </div>
  );
}

/** The table of configuration versions, each uploaded one able to start a run. */
function ConfigVersionTable({
  workspaceId,
  versions,
  connected,
  runsKey,
}: {
  workspaceId: string;
  versions: ConfigVersion[];
  connected: boolean;
  runsKey: string;
}): React.ReactElement {
  if (versions.length === 0) {
    return (
      <p className="text-sm text-surface-300">No configuration versions yet.</p>
    );
  }
  return (
    <div className="overflow-hidden rounded-lg border border-surface-700">
      <table className="w-full text-left text-sm">
        <thead className="bg-surface-800 text-xs text-surface-400">
          <tr>
            <th className="px-3 py-2 font-medium">Version</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Size</th>
            <th className="px-3 py-2 font-medium">Uploaded</th>
            <th className="px-3 py-2" />
          </tr>
        </thead>
        <tbody>
          {versions.map((version) => (
            <tr
              key={version.config_version_id}
              className="border-t border-surface-700"
            >
              <td className="px-3 py-2 font-mono text-xs text-surface-100">
                {version.config_version_id}
              </td>
              <td className="px-3 py-2 text-surface-300">{version.status}</td>
              <td className="px-3 py-2 font-mono text-xs text-surface-300">
                {formatBytes(version.size_bytes)}
              </td>
              <td className="px-3 py-2 text-surface-400">
                {formatDateTime(version.created_at)}
              </td>
              <td className="px-3 py-2 text-right">
                {version.status === 'uploaded' ? (
                  <StartRunButtons
                    workspaceId={workspaceId}
                    configVersionId={version.config_version_id}
                    connected={connected}
                    runsKey={runsKey}
                  />
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The plan-only and plan-and-apply buttons for one configuration version. */
function StartRunButtons({
  workspaceId,
  configVersionId,
  connected,
  runsKey,
}: {
  workspaceId: string;
  configVersionId: string;
  connected: boolean;
  runsKey: string;
}): React.ReactElement {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const start = async (planOnly: boolean): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({
        workspace_id: workspaceId,
        config_version_id: configVersionId,
        plan_only: planOnly,
      });
      invalidateQueries([runsKey]);
      void navigate(`/runs/${run.run_id}`);
    } catch (thrown) {
      setError(
        isRunRoleMissing(thrown) ? new Error(RUN_ROLE_MISSING_MESSAGE) : thrown
      );
    } finally {
      setBusy(false);
    }
  };

  const reason = connected ? undefined : RUN_ROLE_MISSING_MESSAGE;

  return (
    <span className="inline-flex flex-wrap items-center justify-end gap-2">
      <ErrorNotice error={error} />
      {busy ? <Spinner label="Starting the run" className="size-3.5" /> : null}
      <Button
        size="sm"
        disabled={busy || !connected}
        title={reason}
        onClick={() => {
          void start(true);
        }}
      >
        Plan only
      </Button>
      <Button
        size="sm"
        variant="primary"
        disabled={busy || !connected}
        title={reason}
        onClick={() => {
          void start(false);
        }}
      >
        Plan and apply
      </Button>
    </span>
  );
}
