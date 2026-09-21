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
      className={`flex items-start gap-2 rounded-md border border-danger-line bg-danger-soft px-3 py-2 text-sm text-danger ${className}`}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className="mt-0.5 size-4 shrink-0 text-danger"
        fill="none"
      >
        <circle
          cx="8"
          cy="8"
          r="6.25"
          stroke="currentColor"
          strokeWidth="1.5"
        />
        <path
          d="M8 4.75v3.75M8 11.25h.01"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      </svg>
      <span>{describeError(error)}</span>
    </p>
  );
}
