/** The run state badge, coloured by the gating module's tone. */

import { runStateLabel, runTone, type RunState, type RunTone } from '../api';

/** The classes for each tone. */
const TONE_CLASSES: Record<RunTone, string> = {
  neutral: 'bg-surface-700 text-surface-200',
  running: 'bg-brand-600/30 text-brand-300',
  attention: 'bg-amber-500/20 text-amber-300',
  success: 'bg-emerald-500/20 text-emerald-300',
  danger: 'bg-rose-500/20 text-rose-300',
};

/** Props for {@link StateBadge}. */
export interface StateBadgeProps {
  state: RunState;
  className?: string;
}

/** A pill showing a run's state. */
export function StateBadge({
  state,
  className = '',
}: StateBadgeProps): React.ReactElement {
  return (
    <span
      data-testid="run-state-badge"
      data-state={state}
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${TONE_CLASSES[runTone(state)]} ${className}`}
    >
      {runStateLabel(state)}
    </span>
  );
}
