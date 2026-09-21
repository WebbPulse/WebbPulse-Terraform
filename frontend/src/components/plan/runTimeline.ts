/** The stages a run passes through, and where it stands in them. */

import type { Run, RunState } from '../../api';

/** One stage of the run's progress. */
export type StageId =
  | 'plan_queued'
  | 'planning'
  | 'plan_finished'
  | 'awaiting_confirmation'
  | 'applying'
  | 'applied';

/** Whether a stage is behind, at or ahead of where the run stands. */
export type StageStatus =
  'done' | 'current' | 'upcoming' | 'errored' | 'skipped';

/** One row of the timeline. */
export interface Stage {
  id: StageId;
  label: string;
  status: StageStatus;
}

/** The stage labels, in the order the timeline lists them. */
const LABELS: Record<StageId, string> = {
  plan_queued: 'Plan queued',
  planning: 'Planning',
  plan_finished: 'Plan finished',
  awaiting_confirmation: 'Awaiting confirmation',
  applying: 'Apply',
  applied: 'Applied',
};

/** The order stages appear in. */
const ORDER: readonly StageId[] = [
  'plan_queued',
  'planning',
  'plan_finished',
  'awaiting_confirmation',
  'applying',
  'applied',
];

/** How far along a run in each state is, as an index into {@link ORDER}. */
const REACHED: Record<RunState, number> = {
  pending: 0,
  planning: 1,
  planned: 2,
  awaiting_confirmation: 3,
  applying: 4,
  applied: 5,
  planned_and_finished: 2,
  errored: 2,
  cancelled: 2,
  discarded: 3,
};

/**
 * The timeline for a run.
 *
 * A plan only run stops at the plan, so the apply stages are dropped rather
 * than shown as forever upcoming. A run that errored marks the stage it
 * stopped in, and a discarded one marks the confirmation it never got.
 *
 * Terminal states beyond the run's own stage read as skipped, so a cancelled
 * run does not claim to have applied.
 */
export function runStages(run: Run): readonly Stage[] {
  const reached = REACHED[run.status];
  const ids = ORDER.filter((id) => {
    if (!run.plan_only) {
      return true;
    }
    return (
      id !== 'awaiting_confirmation' && id !== 'applying' && id !== 'applied'
    );
  });
  const settled =
    run.status === 'applied' ||
    run.status === 'planned_and_finished' ||
    run.status === 'errored' ||
    run.status === 'cancelled' ||
    run.status === 'discarded';
  const failed = run.status === 'errored';
  const stopped = run.status === 'cancelled' || run.status === 'discarded';

  return ids.map((id) => {
    const index = ORDER.indexOf(id);
    let status: StageStatus;
    if (index < reached) {
      status = 'done';
    } else if (index === reached) {
      if (failed) {
        status = 'errored';
      } else if (stopped) {
        status = 'skipped';
      } else {
        status = settled ? 'done' : 'current';
      }
    } else {
      status = settled ? 'skipped' : 'upcoming';
    }
    return { id, label: LABELS[id], status };
  });
}
