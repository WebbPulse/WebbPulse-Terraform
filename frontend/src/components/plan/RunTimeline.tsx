/** The vertical list of stages a run passes through. */

import type { Run } from '../../api';
import { runStages, type StageStatus } from './runTimeline';

/** Props for {@link RunTimeline}. */
export interface RunTimelineProps {
  run: Run;
  className?: string;
}

/** The classes for each stage status: the marker, its ring and its label. */
const STATUS_CLASSES: Record<
  StageStatus,
  { marker: string; label: string; line: string }
> = {
  done: {
    marker: 'border-success bg-success text-accent-contrast',
    label: 'text-text',
    line: 'bg-success/40',
  },
  current: {
    marker: 'border-running bg-running-soft text-running',
    label: 'font-medium text-text-strong',
    line: 'bg-line',
  },
  upcoming: {
    marker: 'border-line-strong bg-panel text-text-faint',
    label: 'text-text-faint',
    line: 'bg-line',
  },
  errored: {
    marker: 'border-danger bg-danger-soft text-danger',
    label: 'font-medium text-danger',
    line: 'bg-line',
  },
  skipped: {
    marker: 'border-line bg-panel text-text-faint',
    label: 'text-text-faint line-through decoration-line-strong',
    line: 'bg-line',
  },
};

/**
 * The run's stages, the one it is in picked out.
 *
 * An ordered list rather than a row of icons, so a screen reader reads the
 * stages in order with each one's state named beside it.
 */
export function RunTimeline({
  run,
  className = '',
}: RunTimelineProps): React.ReactElement {
  const stages = runStages(run);
  return (
    <ol
      aria-label="Run progress"
      data-testid="run-timeline"
      className={`space-y-0 ${className}`}
    >
      {stages.map((stage, index) => {
        const classes = STATUS_CLASSES[stage.status];
        const last = index === stages.length - 1;
        return (
          <li
            key={stage.id}
            data-stage={stage.id}
            data-status={stage.status}
            aria-current={stage.status === 'current' ? 'step' : undefined}
            className="flex gap-3"
          >
            <div className="flex shrink-0 flex-col items-center">
              <span
                aria-hidden="true"
                className={`flex size-5 items-center justify-center rounded-full border text-[10px] ${classes.marker} ${
                  stage.status === 'current' ? 'animate-pulse' : ''
                }`}
              >
                {stage.status === 'done' ? (
                  <Tick />
                ) : stage.status === 'errored' ? (
                  '!'
                ) : (
                  <span className="size-1.5 rounded-full bg-current" />
                )}
              </span>
              {last ? null : (
                <span
                  aria-hidden="true"
                  className={`w-px flex-1 ${classes.line}`}
                />
              )}
            </div>
            <span className={`pb-4 text-sm ${classes.label}`}>
              {stage.label}
              <span className="sr-only">{`, ${statusWords(stage.status)}`}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** The words a screen reader hears for a stage's status. */
function statusWords(status: StageStatus): string {
  switch (status) {
    case 'done':
      return 'finished';
    case 'current':
      return 'in progress';
    case 'upcoming':
      return 'not started';
    case 'errored':
      return 'errored';
    case 'skipped':
      return 'not reached';
  }
}

/** The tick inside a finished stage's marker. */
function Tick(): React.ReactElement {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true" className="size-3" fill="none">
      <path
        d="m2.5 6.25 2.25 2.25L9.5 3.75"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
