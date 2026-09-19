/**
 * The upload form, shared by the checklist and the configuration versions tab.
 *
 * Two legs: the API mints a configuration version and a presigned PUT, then the
 * tarball goes straight to S3 on that URL, so it never passes through Lambda.
 *
 * The file's own size is declared up front because the presigned URL signs it
 * as `Content-Length`, and S3 enforces that at the header.
 */

import { useRef, useState } from 'react';
import { invalidateQueries } from '@webbpulse/api-client/react';

import { api, uploadConfigTarball } from '../../api';
import { Button, ErrorNotice, Field } from '../../components';

/** Props for {@link UploadConfigForm}. */
export interface UploadConfigFormProps {
  workspaceId: string;
  /** The refetch key of the configuration versions list to invalidate. */
  queryKey: string;
  /** The form's accessible name. */
  label: string;
  /** The file input's label. */
  fileLabel: string;
  /** One sentence under the input, if any. */
  hint?: string;
  onUploaded?: () => void;
}

/** The form that mints a version and PUTs the file to its presigned URL. */
export function UploadConfigForm({
  workspaceId,
  queryKey,
  label,
  fileLabel,
  hint,
  onUploaded,
}: UploadConfigFormProps): React.ReactElement {
  const input = useRef<HTMLInputElement>(null);
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
        size_bytes: file.size,
      });
      await uploadConfigTarball(upload, file);
      invalidateQueries([queryKey]);
      setDone(true);
      if (input.current !== null) {
        input.current.value = '';
      }
      onUploaded?.();
    } catch (thrown) {
      setError(thrown);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      aria-label={label}
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="flex flex-wrap items-end gap-3">
        <Field label={fileLabel} hint={hint} className="min-w-0 flex-1">
          {(control) => (
            <input
              {...control}
              ref={input}
              type="file"
              required
              accept=".tar.gz,application/gzip"
              className="mt-1 block w-full text-sm text-text file:mr-3 file:h-7 file:rounded-md file:border file:border-line-strong file:bg-panel file:px-2.5 file:text-xs file:font-medium file:text-text hover:file:bg-raised"
            />
          )}
        </Field>
        <Button
          type="submit"
          variant="primary"
          busy={busy}
          busyLabel="Uploading the configuration"
        >
          Upload
        </Button>
      </div>
      {done ? (
        <p role="status" className="text-sm text-emerald-300">
          Uploaded.
        </p>
      ) : null}
      <ErrorNotice error={error} />
    </form>
  );
}
