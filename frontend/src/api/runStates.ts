/** What a run's state allows, and how to render it. */

import type { Run, RunState } from './types';

/** The states a run is still working through, so a detail view keeps polling. */
const ACTIVE_STATES: readonly RunState[] = [
  'pending',
  'planning',
  'awaiting_confirmation',
  'applying',
];

/** The states a run never leaves. */
const TERMINAL_STATES: readonly RunState[] = [
  'applied',
  'planned_and_finished',
  'errored',
  'cancelled',
  'discarded',
];

/** Whether the run is still moving, which is what polling is gated on. */
export function isActive(state: RunState): boolean {
  return ACTIVE_STATES.includes(state);
}

/** Whether the run has settled and will not change again. */
export function isTerminal(state: RunState): boolean {
  return TERMINAL_STATES.includes(state);
}

/**
 * Whether confirming an apply is allowed.
 *
 * Only a plan that finished and is waiting on a person: a `plan_only` run has
 * nothing to confirm, and `planned` alone means the state machine has not
 * stored its confirmation task token yet.
 */
export function canConfirm(run: Run): boolean {
  return run.state === 'awaiting_confirmation' && !run.plan_only;
}

/** The states that hold a finished plan a person can throw away. */
const DISCARDABLE_STATES: readonly RunState[] = [
  'planned',
  'awaiting_confirmation',
];

/**
 * Whether discarding is allowed.
 *
 * Discard throws away a plan nobody will apply, so it covers a run holding a
 * finished plan, whether or not the state machine has stored its confirmation
 * task token yet.
 */
export function canDiscard(run: Run): boolean {
  return DISCARDABLE_STATES.includes(run.state);
}

/** The states with a phase running or queued that cancel can stop. */
const CANCELLABLE_STATES: readonly RunState[] = [
  'pending',
  'planning',
  'applying',
];

/**
 * Whether cancelling is allowed.
 *
 * Cancel only stops a phase that is running or queued. A run holding a
 * finished plan is discarded instead.
 */
export function canCancel(run: Run): boolean {
  return CANCELLABLE_STATES.includes(run.state);
}

/** How a state badge is coloured. */
export type RunTone =
  'neutral' | 'running' | 'attention' | 'success' | 'danger';

/** The tone for each state, so the badge and the list agree on colour. */
const TONES: Record<RunState, RunTone> = {
  pending: 'neutral',
  planning: 'running',
  planned: 'running',
  awaiting_confirmation: 'attention',
  applying: 'running',
  applied: 'success',
  planned_and_finished: 'success',
  errored: 'danger',
  cancelled: 'neutral',
  discarded: 'neutral',
};

/** The badge tone for a state. */
export function runTone(state: RunState): RunTone {
  return TONES[state];
}

/** The sentence a state badge shows. */
const LABELS: Record<RunState, string> = {
  pending: 'Pending',
  planning: 'Planning',
  planned: 'Planned',
  awaiting_confirmation: 'Needs confirmation',
  applying: 'Applying',
  applied: 'Applied',
  planned_and_finished: 'Planned and finished',
  errored: 'Errored',
  cancelled: 'Cancelled',
  discarded: 'Discarded',
};

/** The human label for a state. */
export function runStateLabel(state: RunState): string {
  return LABELS[state];
}

/**
 * Which phase's logs to show by default.
 *
 * The apply phase once the run reached it, otherwise the plan, so opening a
 * run lands on the phase that is actually producing output.
 */
export function defaultPhase(state: RunState): 'plan' | 'apply' {
  return state === 'applying' || state === 'applied' ? 'apply' : 'plan';
}

/** Whether the run got far enough for an apply log stream to exist. */
export function hasApplyPhase(state: RunState): boolean {
  return state === 'applying' || state === 'applied';
}
