/** An expandable section with a title row, as a run page stacks its phases. */

import { useId, type ReactNode } from 'react';

/** Props for {@link Disclosure}. */
export interface DisclosureProps {
  title: string;
  /** Words at the right of the title row, such as a phase's resource counts. */
  summary?: ReactNode;
  open: boolean;
  onToggle: () => void;
  /** A dot beside the title, coloured for the phase's state. */
  tone?: 'neutral' | 'running' | 'attention' | 'success' | 'danger';
  children: ReactNode;
  className?: string;
}

const DOT_CLASSES: Record<NonNullable<DisclosureProps['tone']>, string> = {
  neutral: 'bg-surface-500',
  running: 'bg-brand-400 animate-pulse',
  attention: 'bg-amber-400',
  success: 'bg-emerald-400',
  danger: 'bg-rose-400',
};

/** A section whose body shows only while open, so a collapsed phase does no work. */
export function Disclosure({
  title,
  summary,
  open,
  onToggle,
  tone = 'neutral',
  children,
  className = '',
}: DisclosureProps): React.ReactElement {
  const bodyId = useId();
  return (
    <section
      className={`rounded-lg border border-line bg-panel ${className}`}
      data-open={open}
    >
      <h3 className="m-0">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={onToggle}
          className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-4 py-3 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
          <span
            aria-hidden="true"
            className={`size-2 shrink-0 rounded-full ${DOT_CLASSES[tone]}`}
          />
          <span className="font-medium text-text-strong">{title}</span>
          {summary === undefined ? null : (
            <span className="ml-auto text-xs text-text-muted">{summary}</span>
          )}
        </button>
      </h3>
      {open ? (
        <div id={bodyId} className="border-t border-line px-4 py-4">
          {children}
        </div>
      ) : null}
    </section>
  );
}
