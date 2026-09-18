/** The configuration versions tab: upload a tarball, then start a run from it. */

import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { invalidateQueries, usePolledQuery } from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

import {
  api,
  uploadConfigTarball,
  type ConfigVersion,
  type ConfigVersionList,
} from '../../api';
import { ErrorNotice, Spinner } from '../../components';

/** Props for {@link ConfigVersionsTab}. */
export interface ConfigVersionsTabProps {
  workspaceId: string;
}

/** The configuration versions list and its upload form. */
export function ConfigVersionsTab({
  workspaceId,
}: ConfigVersionsTabProps): React.ReactElement {
  const auth = useAuthClient();
  const queryKey = `config-versions:${workspaceId}`;
  const query = usePolledQuery<ConfigVersionList>(
    ({ signal }) => api.listConfigVersions(workspaceId, { signal }),
    { intervalMs: 30_000, queryKey, auth }
  );

  return (
    <div className="space-y-5">
      <ErrorNotice error={query.error} />
      <UploadForm workspaceId={workspaceId} queryKey={queryKey} />
      {query.isLoading ? (
        <Spinner label="Loading configuration versions" />
      ) : (
        <ConfigVersionTable
          workspaceId={workspaceId}
          versions={query.data?.config_versions ?? []}
        />
      )}
    </div>
  );
}

/** The table of configuration versions, each uploaded one able to start a run. */
function ConfigVersionTable({
  workspaceId,
  versions,
}: {
  workspaceId: string;
  versions: ConfigVersion[];
}): React.ReactElement {
  if (versions.length === 0) {
    return <p className="text-surface-300">No configuration versions yet.</p>;
  }
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-surface-400">
        <tr>
          <th className="py-2">Id</th>
          <th className="py-2">Status</th>
          <th className="py-2">Message</th>
          <th className="py-2" />
        </tr>
      </thead>
      <tbody>
        {versions.map((version) => (
          <tr
            key={version.config_version_id}
            className="border-t border-surface-700"
          >
            <td className="py-2 font-mono">{version.config_version_id}</td>
            <td className="py-2 text-surface-300">{version.status}</td>
            <td className="py-2 text-surface-300">{version.message ?? ''}</td>
            <td className="py-2 text-right">
              {version.status === 'uploaded' ? (
                <StartRunButtons
                  workspaceId={workspaceId}
                  configVersionId={version.config_version_id}
                />
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The plan-only and plan-and-apply buttons for one configuration version. */
function StartRunButtons({
  workspaceId,
  configVersionId,
}: {
  workspaceId: string;
  configVersionId: string;
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
      invalidateQueries([`runs:${workspaceId}`]);
      void navigate(`/runs/${run.run_id}`);
    } catch (thrown) {
      setError(thrown);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="inline-flex items-center gap-3">
      <ErrorNotice error={error} />
      {busy ? <Spinner label="Starting the run" /> : null}
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          void start(true);
        }}
        className="text-brand-300 hover:text-brand-200 disabled:opacity-60"
      >
        Plan only
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          void start(false);
        }}
        className="text-brand-300 hover:text-brand-200 disabled:opacity-60"
      >
        Plan and apply
      </button>
    </span>
  );
}

/**
 * The upload form.
 *
 * Two legs: the API mints a configuration version and a presigned PUT, then the
 * tarball goes straight to S3 on that URL, so it never passes through Lambda.
 */
function UploadForm({
  workspaceId,
  queryKey,
}: {
  workspaceId: string;
  queryKey: string;
}): React.ReactElement {
  const input = useRef<HTMLInputElement>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);

  const submit = async (): Promise<void> => {
    const file = input.current?.files?.[0];
    if (file === undefined) {
      return;
    }
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      const upload = await api.createConfigVersion(workspaceId, {
        ...(message === '' ? {} : { message }),
      });
      await uploadConfigTarball(upload, file);
      invalidateQueries([queryKey]);
      setDone(true);
      setMessage('');
      if (input.current !== null) {
        input.current.value = '';
      }
    } catch (thrown) {
      setError(thrown);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      aria-label="Upload a configuration version"
      className="flex flex-wrap items-end gap-3 rounded-lg border border-surface-700 bg-surface-800 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <label className="text-sm">
        <span className="block text-surface-300">Configuration tarball</span>
        <input
          ref={input}
          type="file"
          required
          accept=".tar.gz,application/gzip"
          className="mt-1 text-surface-200"
        />
      </label>
      <label className="text-sm">
        <span className="block text-surface-300">Message</span>
        <input
          value={message}
          onChange={(event) => {
            setMessage(event.target.value);
          }}
          className="mt-1 rounded-md border border-surface-600 bg-surface-900 px-3 py-2"
        />
      </label>
      <button
        type="submit"
        disabled={busy}
        className="flex items-center gap-2 rounded-md bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
      >
        {busy ? <Spinner label="Uploading the configuration" /> : null}
        Upload
      </button>
      {done ? (
        <p role="status" className="w-full text-sm text-emerald-300">
          Uploaded.
        </p>
      ) : null}
      <ErrorNotice error={error} className="w-full" />
    </form>
  );
}
