/** The dashed box a list shows when it has nothing to list. */

import type { ReactNode } from 'react';

/** Props for {@link EmptyState}. */
export interface EmptyStateProps {
  /** The one sentence the tests and the eye look for. */
  title: string;
  /** What would put something here. */
  hint?: string;
  action?: ReactNode;
  className?: string;
}

/** An empty list, said plainly. */
export function EmptyState({
  title,
  hint,
  action,
  className = '',
}: EmptyStateProps): React.ReactElement {
  return (
    <div
      className={`rounded-lg border border-dashed border-line px-4 py-10 text-center ${className}`}
    >
      <p className="text-sm text-text">{title}</p>
      {hint === undefined ? null : (
        <p className="mt-1 text-xs text-text-faint">{hint}</p>
      )}
      {action === undefined ? null : <div className="mt-4">{action}</div>}
    </div>
  );
}
