/** A labelled control with an optional hint and inline error. */

import { useId, type ReactNode } from 'react';

/** The classes every text input, select and textarea shares. */
export const INPUT_CLASS =
  'mt-1 block h-8 w-full rounded-md border border-line-strong bg-bg px-2.5 text-sm text-text placeholder:text-surface-500 transition-colors hover:border-surface-500 focus-visible:border-accent focus-visible:ring-1 focus-visible:ring-accent focus-visible:outline-none read-only:text-text-faint aria-[invalid=true]:border-rose-500 disabled:cursor-not-allowed disabled:opacity-50';

/** Props for {@link Field}. */
export interface FieldProps {
  label: string;
  /** One sentence under the control. */
  hint?: ReactNode;
  /** The inline error, when the value is refused. */
  error?: string | null;
  className?: string;
  /** Renders the control given its accessibility wiring. */
  children: (control: {
    id: string;
    'aria-describedby': string | undefined;
    'aria-invalid': boolean;
  }) => ReactNode;
}

/** A label, the control and its hint or error, wired together. */
export function Field({
  label,
  hint,
  error = null,
  className = '',
  children,
}: FieldProps): React.ReactElement {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy =
    [hint === undefined ? null : hintId, error === null ? null : errorId]
      .filter((value) => value !== null)
      .join(' ') || undefined;
  return (
    <div className={`text-sm ${className}`}>
      <label htmlFor={id} className="text-text-muted">
        {label}
      </label>
      {children({
        id,
        'aria-describedby': describedBy,
        'aria-invalid': error !== null,
      })}
      {hint === undefined ? null : (
        <p id={hintId} className="mt-1 text-xs text-text-faint">
          {hint}
        </p>
      )}
      {error === null ? null : (
        <p id={errorId} role="alert" className="mt-1 text-xs text-rose-300">
          {error}
        </p>
      )}
    </div>
  );
}
