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

/** How long the whole upload may take before it is abandoned with an error. */
export const UPLOAD_DEADLINE_MS = 120_000;

/** The error an upload that outlived {@link UPLOAD_DEADLINE_MS} shows. */
const UPLOAD_TIMED_OUT = new Error(
  'The upload timed out. Check the connection and try again.'
);

/** The error a submit without a chosen file shows. */
const NO_FILE = new Error('Choose a .tar.gz configuration archive first.');

/**
 * The form that mints a version and PUTs the file to its presigned URL.
 *
 * Every way a submit can end is shown: no file, a refused mint, a failed or
 * stalled PUT, and success. A ref guards against a second submit landing before
 * the busy state renders, and the whole attempt runs under a deadline so a hung
 * request cannot leave the form spinning.
 */
export function UploadConfigForm({
  workspaceId,
  queryKey,
  label,
  fileLabel,
  hint,
  onUploaded,
}: UploadConfigFormProps): React.ReactElement {
  const input = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [uploaded, setUploaded] = useState<string | null>(null);

  const submit = async (): Promise<void> => {
    if (inFlight.current) {
      return;
    }
    const file = input.current?.files?.[0];
    if (file === undefined) {
      setUploaded(null);
      setError(NO_FILE);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setUploaded(null);
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, UPLOAD_DEADLINE_MS);
    try {
      const upload = await api.createConfigVersion(
        workspaceId,
        { size_bytes: file.size },
        { signal: controller.signal }
      );
      await uploadConfigTarball(upload, file, { signal: controller.signal });
      invalidateQueries(queryKey);
      setUploaded(file.name);
      if (input.current !== null) {
        input.current.value = '';
      }
      onUploaded?.();
    } catch (thrown) {
      setError(controller.signal.aborted ? UPLOAD_TIMED_OUT : thrown);
    } finally {
      clearTimeout(timer);
      inFlight.current = false;
      setBusy(false);
    }
  };

  return (
    <form
      aria-label={label}
      noValidate
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
              disabled={busy}
              accept=".tar.gz,application/gzip"
              className="mt-1 block w-full text-sm text-text file:mr-3 file:h-7 file:rounded-md file:border file:border-line-strong file:bg-panel file:px-2.5 file:text-xs file:font-medium file:text-text-strong hover:file:bg-raised"
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
      {uploaded === null ? null : (
        <p role="status" className="text-sm text-success">
          Uploaded {uploaded}.
        </p>
      )}
      <ErrorNotice error={error} />
    </form>
  );
}
