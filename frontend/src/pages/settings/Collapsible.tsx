/** A row that opens to show more, with the chevron every collapsible in the app uses. */

import { useId, type ReactNode } from 'react';

/** Props for {@link Collapsible}. */
export interface CollapsibleProps {
  title: string;
  /** Words at the right of the row, such as a count. */
  summary?: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}

/** A bordered row whose body renders only while open, so a closed one fetches nothing. */
export function Collapsible({
  title,
  summary,
  open,
  onToggle,
  children,
}: CollapsibleProps): React.ReactElement {
  const bodyId = useId();
  return (
    <div className="rounded-md border border-line">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text hover:bg-raised/60 hover:text-text-strong focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
      >
        <svg
          viewBox="0 0 16 16"
          aria-hidden="true"
          className={`size-3.5 shrink-0 text-text-faint transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none"
        >
          <path
            d="m6 3 5 5-5 5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span>{title}</span>
        {summary === undefined ? null : (
          <span className="ml-auto text-xs text-text-faint">{summary}</span>
        )}
      </button>
      {open ? (
        <div id={bodyId} className="border-t border-line px-3 py-3">
          {children}
        </div>
      ) : null}
    </div>
  );
}
