/** A run's raw log, with a tab per phase so the plan survives the apply. */

import { useState } from 'react';

import { applyPhaseStatus, type Run, type RunPhase } from '../api';
import { RunLogViewer } from './RunLogViewer';
import { SegmentedControl, type Segment } from './SegmentedControl';

/** Props for {@link RunLogs}. */
export interface RunLogsProps {
  run: Run;
}

/** Whether the apply phase has a log to read yet. */
function applyStarted(run: Run): boolean {
  const apply = applyPhaseStatus(run);
  return apply !== null && apply !== 'pending' && apply !== 'discarded';
}

/** Whether a phase is still writing its log. */
function phaseLive(run: Run, phase: RunPhase): boolean {
  if (phase === 'apply') {
    return run.status === 'applying';
  }
  return run.status === 'pending' || run.status === 'planning';
}

/**
 * The raw log of each phase a run reached.
 *
 * Opens on the latest phase and follows the run into its apply until a phase is
 * picked by hand. The plan tab stays readable once the apply has started, since
 * the plan's transcript is what the apply was confirmed against.
 */
export function RunLogs({ run }: RunLogsProps): React.ReactElement {
  const [picked, setPicked] = useState<RunPhase | null>(null);
  const hasApply = applyStarted(run);
  const latest: RunPhase = hasApply ? 'apply' : 'plan';
  const phase: RunPhase = hasApply ? (picked ?? latest) : 'plan';
  const segments: Segment<RunPhase>[] = hasApply
    ? [
        { id: 'plan', label: 'Plan log' },
        { id: 'apply', label: 'Apply log' },
      ]
    : [{ id: 'plan', label: 'Plan log' }];

  return (
    <div data-testid="run-log-phases" data-phase={phase}>
      <RunLogViewer
        runId={run.run_id}
        phase={phase}
        live={phaseLive(run, phase)}
        controls={
          <SegmentedControl<RunPhase>
            label="Log phase"
            segments={segments}
            value={phase}
            onChange={setPicked}
          />
        }
      />
    </div>
  );
}
