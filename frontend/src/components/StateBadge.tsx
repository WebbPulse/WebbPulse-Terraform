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
    pill: 'border-line-strong bg-raised text-text',
    dot: 'bg-surface-500',
  },
  running: {
    pill: 'border-running-line bg-running-soft text-running',
    dot: 'bg-running',
  },
  attention: {
    pill: 'border-warning-line bg-warning-soft text-warning',
    dot: 'bg-warning',
  },
  success: {
    pill: 'border-success-line bg-success-soft text-success',
    dot: 'bg-success',
  },
  danger: {
    pill: 'border-danger-line bg-danger-soft text-danger',
    dot: 'bg-danger',
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
