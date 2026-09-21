/** The counted sentence above a plan's resource list. */

import type { RunPlan } from '../../api/runPlan';

/** Props for {@link PlanSummaryLine}. */
export interface PlanSummaryLineProps {
  plan: Pick<RunPlan, 'changes' | 'has_changes' | 'resource_changes'>;
  className?: string;
}

/** How many resources the plan replaces, which the counts fold into change. */
function replaceCount(plan: PlanSummaryLineProps['plan']): number {
  return (plan.resource_changes ?? []).filter(
    (change) => change.action === 'replace'
  ).length;
}

/**
 * The plan's counts, or the sentence for a plan with nothing to do.
 *
 * Replacements are counted separately beside the three the engine reports,
 * because a replacement destroys and recreates and reading it as a change
 * understates what the apply will do.
 */
export function PlanSummaryLine({
  plan,
  className = '',
}: PlanSummaryLineProps): React.ReactElement {
  if (!plan.has_changes) {
    return (
      <p
        data-testid="plan-summary-line"
        className={`text-sm text-text-muted ${className}`}
      >
        No changes. Your infrastructure matches the configuration.
      </p>
    );
  }
  const replaces = replaceCount(plan);
  return (
    <p
      data-testid="plan-summary-line"
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-sm tabular-nums ${className}`}
    >
      <Count value={plan.changes.add ?? 0} word="to add" className="text-add" />
      <Count
        value={plan.changes.change ?? 0}
        word="to change"
        className="text-change"
      />
      <Count
        value={plan.changes.destroy ?? 0}
        word="to destroy"
        className="text-destroy"
      />
      {replaces === 0 ? null : (
        <Count
          value={replaces}
          word="to replace"
          className="text-replace"
          testId="plan-replace-count"
        />
      )}
    </p>
  );
}

/** One coloured count and the words after it. */
function Count({
  value,
  word,
  className,
  testId,
}: {
  value: number;
  word: string;
  className: string;
  testId?: string;
}): React.ReactElement {
  return (
    <span data-testid={testId} className={className}>
      <span className="font-semibold">{value}</span>{' '}
      <span className="text-text-muted">{word}</span>
    </span>
  );
}
