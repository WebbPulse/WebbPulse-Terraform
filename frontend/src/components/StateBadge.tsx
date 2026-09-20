/** The run state badge, coloured by the gating module's tone. */

import {
  isActive,
  runStateLabel,
  runTone,
  type RunState,
  type RunTone,
} from '../api';

/** The classes for each tone. */
const TONE_CLASSES: Record<RunTone, { pill: string; dot: string }> = {
  neutral: {
    pill: 'border-surface-600 bg-surface-800 text-surface-200',
    dot: 'bg-surface-400',
  },
  running: {
    pill: 'border-brand-500/40 bg-brand-600/15 text-brand-300',
    dot: 'bg-brand-400',
  },
  attention: {
    pill: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
    dot: 'bg-amber-400',
  },
  success: {
    pill: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
    dot: 'bg-emerald-400',
  },
  danger: {
    pill: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
    dot: 'bg-rose-400',
  },
};

/** Props for {@link StateBadge}. */
export interface StateBadgeProps {
  state: RunState;
  className?: string;
}

/** A pill showing a run's state, with a pulsing dot while it is moving. */
export function StateBadge({
  state,
  className = '',
}: StateBadgeProps): React.ReactElement {
  const tone = TONE_CLASSES[runTone(state)];
  return (
    <span
      data-testid="run-state-badge"
      data-state={state}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap ${tone.pill} ${className}`}
    >
      <span
        aria-hidden="true"
        className={`size-1.5 rounded-full ${tone.dot} ${
          isActive(state) ? 'animate-pulse' : ''
        }`}
      />
      {runStateLabel(state)}
    </span>
  );
}
