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
  EmptyState,
  ErrorNotice,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
  formatBytes,
  formatDateTime,
  formatRelative,
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
      <div className="rounded-lg border border-line bg-panel p-4">
        <UploadConfigForm
          workspaceId={workspace.workspace_id}
          queryKey={keys.versions}
          label="Upload a configuration version"
          fileLabel="Configuration tarball"
        />
      </div>
      {connected ? null : (
        <p
          role="note"
          className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
        >
          {RUN_ROLE_MISSING_MESSAGE} Runs stay disabled until the connection
          check passes on the Overview tab.
        </p>
      )}
      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading configuration versions" className="size-4" />
          Loading configuration versions
        </div>
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
      <EmptyState
        title="No configuration versions yet."
        hint="Upload a tar.gz of your root module to start a run from it."
      />
    );
  }
  return (
    <Table label="Configuration versions">
      <thead>
        <tr>
          <Th>Version</Th>
          <Th>Status</Th>
          <Th>Size</Th>
          <Th>Uploaded</Th>
          <Th className="text-right">Start a run</Th>
        </tr>
      </thead>
      <tbody>
        {versions.map((version) => (
          <Tr key={version.config_version_id}>
            <Td className="font-mono text-xs text-text-strong">
              {version.config_version_id}
            </Td>
            <Td>
              <VersionStatus status={version.status} />
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {formatBytes(version.size_bytes)}
            </Td>
            <Td
              className="text-xs whitespace-nowrap text-text-faint"
              title={formatDateTime(version.created_at)}
            >
              {formatRelative(version.created_at)}
            </Td>
            <Td className="text-right">
              {version.status === 'uploaded' ? (
                <StartRunButtons
                  workspaceId={workspaceId}
                  configVersionId={version.config_version_id}
                  connected={connected}
                  runsKey={runsKey}
                />
              ) : (
                <span className="text-xs text-text-faint">Not uploaded</span>
              )}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/** The version's status as a small pill. */
function VersionStatus({
  status,
}: {
  status: ConfigVersion['status'];
}): React.ReactElement {
  const uploaded = status === 'uploaded';
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
        uploaded
          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
          : 'border-surface-600 bg-surface-800 text-surface-200'
      }`}
    >
      <span
        aria-hidden="true"
        className={`size-1.5 rounded-full ${uploaded ? 'bg-emerald-400' : 'bg-surface-400'}`}
      />
      {status}
    </span>
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
