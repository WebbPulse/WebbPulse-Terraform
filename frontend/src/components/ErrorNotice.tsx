/** The one place a failed request turns into a rendered sentence. */

import { describeError } from '../api';

/** Props for {@link ErrorNotice}. */
export interface ErrorNoticeProps {
  /** The thrown error, or null to render nothing. */
  error: unknown;
  className?: string;
}

/** An alert carrying the error's message, or nothing when there is no error. */
export function ErrorNotice({
  error,
  className = '',
}: ErrorNoticeProps): React.ReactElement | null {
  if (error === null || error === undefined) {
    return null;
  }
  return (
    <p
      role="alert"
      className={`rounded-md bg-rose-500/10 px-3 py-2 text-sm text-rose-300 ${className}`}
    >
      {describeError(error)}
    </p>
  );
}
