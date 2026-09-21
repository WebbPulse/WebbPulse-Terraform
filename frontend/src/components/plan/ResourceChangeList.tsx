/** The resources a plan touches, each expanding to its attribute diff. */

import { useState } from 'react';

import type { PlanResourceChange } from '../../api/runPlan';
import { AttributeDiffTable } from './AttributeDiffTable';
import {
  actionGlyph,
  actionLabel,
  actionTone,
  type ActionTone,
} from './planDiff';

/** Props for {@link ResourceChangeList}. */
export interface ResourceChangeListProps {
  changes: readonly PlanResourceChange[];
}

/** The classes for each action's glyph and its row accent. */
const TONE_CLASSES: Record<ActionTone, { glyph: string; edge: string }> = {
  add: { glyph: 'text-add', edge: 'border-l-add' },
  change: { glyph: 'text-change', edge: 'border-l-change' },
  destroy: { glyph: 'text-destroy', edge: 'border-l-destroy' },
  replace: { glyph: 'text-replace', edge: 'border-l-replace' },
  read: { glyph: 'text-read', edge: 'border-l-read' },
  none: { glyph: 'text-text-faint', edge: 'border-l-line' },
};

/** The resources the plan changes, in the order the engine reported them. */
export function ResourceChangeList({
  changes,
}: ResourceChangeListProps): React.ReactElement {
  if (changes.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
        This plan changes no resources.
      </p>
    );
  }
  return (
    <ul
      aria-label="Resource changes"
      data-testid="resource-changes"
      className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-panel"
    >
      {changes.map((change) => (
        <ResourceChangeRow key={change.address} change={change} />
      ))}
    </ul>
  );
}

/** One resource: its glyph, its address and its diff once opened. */
function ResourceChangeRow({
  change,
}: {
  change: PlanResourceChange;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  const tone = TONE_CLASSES[actionTone(change.action)];
  const hasDiff = change.action !== 'no-op';

  return (
    <li
      data-testid="resource-change"
      data-address={change.address}
      data-action={change.action}
      className={`border-l-2 ${tone.edge}`}
    >
      <button
        type="button"
        aria-expanded={open}
        disabled={!hasDiff}
        onClick={() => {
          setOpen((value) => !value);
        }}
        className="flex w-full flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2 text-left hover:bg-raised/60 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none disabled:cursor-default disabled:hover:bg-transparent"
      >
        <span
          aria-hidden="true"
          className={`w-7 shrink-0 font-mono text-sm font-semibold ${tone.glyph}`}
        >
          {actionGlyph(change.action)}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-sm text-text-strong">
          {change.address}
        </span>
        <span className="text-xs text-text-faint">
          {change.type}
          <span className="sr-only">{`, ${actionLabel(change.action)}`}</span>
        </span>
        {change.action_reason === '' ? null : (
          <span
            data-testid="action-reason"
            className="rounded border border-line-strong px-1.5 py-0.5 text-[10px] text-text-muted"
          >
            {change.action_reason.replaceAll('_', ' ')}
          </span>
        )}
      </button>
      {open && hasDiff ? (
        <div className="border-t border-line bg-bg/40 py-2">
          <AttributeDiffTable change={change} />
        </div>
      ) : null}
    </li>
  );
}
