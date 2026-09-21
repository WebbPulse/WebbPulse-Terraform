/** A row of mutually exclusive options, keyboard navigable as a tab list. */

import type { KeyboardEvent } from 'react';

/** One option of a {@link SegmentedControl}. */
export interface Segment<TId extends string> {
  id: TId;
  label: string;
}

/** Props for {@link SegmentedControl}. */
export interface SegmentedControlProps<TId extends string> {
  segments: readonly Segment<TId>[];
  value: TId;
  onChange: (id: TId) => void;
  /** The accessible name of the group. */
  label: string;
  className?: string;
}

/** A segmented control implemented as a tab list, with arrow key movement. */
export function SegmentedControl<TId extends string>({
  segments,
  value,
  onChange,
  label,
  className = '',
}: SegmentedControlProps<TId>): React.ReactElement {
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const index = segments.findIndex((segment) => segment.id === value);
    if (index === -1) {
      return;
    }
    let next: number;
    if (event.key === 'ArrowRight') {
      next = (index + 1) % segments.length;
    } else if (event.key === 'ArrowLeft') {
      next = (index - 1 + segments.length) % segments.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = segments.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const target = segments[next];
    if (target !== undefined) {
      onChange(target.id);
      const button = event.currentTarget.querySelector<HTMLButtonElement>(
        `[data-segment="${target.id}"]`
      );
      button?.focus();
    }
  };

  return (
    <div
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={`inline-flex rounded-md border border-line bg-raised p-0.5 ${className}`}
    >
      {segments.map((segment) => {
        const selected = segment.id === value;
        return (
          <button
            key={segment.id}
            type="button"
            role="tab"
            data-segment={segment.id}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => {
              onChange(segment.id);
            }}
            className={`rounded px-2.5 py-1 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none ${
              selected
                ? 'bg-panel text-text-strong shadow-xs'
                : 'text-text-muted hover:text-text-strong'
            }`}
          >
            {segment.label}
          </button>
        );
      })}
    </div>
  );
}
