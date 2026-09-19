/** Underline tabs, keyboard navigable, that switch a panel beneath them. */

import type { KeyboardEvent } from 'react';

/** One tab. */
export interface TabItem<TId extends string> {
  id: TId;
  label: string;
  disabled?: boolean;
  /** A small count beside the label. */
  count?: number;
}

/** Props for {@link Tabs}. */
export interface TabsProps<TId extends string> {
  tabs: readonly TabItem<TId>[];
  value: TId;
  onChange: (id: TId) => void;
  /** The accessible name of the list. */
  label: string;
  className?: string;
}

/** A tab list with an underline on the selected tab and arrow key movement. */
export function Tabs<TId extends string>({
  tabs,
  value,
  onChange,
  label,
  className = '',
}: TabsProps<TId>): React.ReactElement {
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const enabled = tabs.filter((tab) => tab.disabled !== true);
    const index = enabled.findIndex((tab) => tab.id === value);
    if (index === -1 || enabled.length === 0) {
      return;
    }
    let next: number;
    if (event.key === 'ArrowRight') {
      next = (index + 1) % enabled.length;
    } else if (event.key === 'ArrowLeft') {
      next = (index - 1 + enabled.length) % enabled.length;
    } else if (event.key === 'Home') {
      next = 0;
    } else if (event.key === 'End') {
      next = enabled.length - 1;
    } else {
      return;
    }
    event.preventDefault();
    const target = enabled[next];
    if (target !== undefined) {
      onChange(target.id);
      event.currentTarget
        .querySelector<HTMLButtonElement>(`[data-tab="${target.id}"]`)
        ?.focus();
    }
  };

  return (
    <div
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={`flex gap-1 border-b border-line ${className}`}
    >
      {tabs.map((tab) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            data-tab={tab.id}
            aria-selected={selected}
            disabled={tab.disabled}
            tabIndex={selected ? 0 : -1}
            onClick={() => {
              onChange(tab.id);
            }}
            className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40 ${
              selected
                ? 'border-accent text-text-strong'
                : 'border-transparent text-text-muted hover:text-text-strong'
            }`}
          >
            {tab.label}
            {tab.count === undefined ? null : (
              <span className="rounded-full bg-raised px-1.5 text-[11px] text-text-muted">
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
