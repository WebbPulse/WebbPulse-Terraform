/** The counted sentence above a plan's resource list. */

import type { PlanResourceChange, RunPlan } from '../../api/runPlan';

/** Props for {@link PlanSummaryLine}. */
export interface PlanSummaryLineProps {
  plan: Pick<RunPlan, 'changes' | 'has_changes' | 'resource_changes'>;
  className?: string;
}

/** How many listed resources match `predicate`. */
function countWhere(
  plan: PlanSummaryLineProps['plan'],
  predicate: (change: PlanResourceChange) => boolean
): number {
  return (plan.resource_changes ?? []).filter(predicate).length;
}

/**
 * The plan's counts, or the sentence for a plan with nothing to do.
 *
 * Replacements are counted separately beside the three the engine reports,
 * because a replacement destroys and recreates and reading it as a change
 * understates what the apply will do. Imports, moves and forgets are counted
 * when present, as Terraform's own summary does, since they change the state
 * without adding, changing or destroying anything.
 */
export function PlanSummaryLine({
  plan,
  className = '',
}: PlanSummaryLineProps): React.ReactElement {
  const replaces = countWhere(plan, (change) => change.action === 'replace');
  const imports = countWhere(plan, (change) => change.importing === true);
  const moves = countWhere(
    plan,
    (change) => (change.previous_address ?? '') !== ''
  );
  const forgets = countWhere(plan, (change) => change.action === 'forget');
  if (!plan.has_changes && imports + moves + forgets === 0) {
    return (
      <p
        data-testid="plan-summary-line"
        className={`text-sm text-text-muted ${className}`}
      >
        No changes. Your infrastructure matches the configuration.
      </p>
    );
  }
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
      {imports === 0 ? null : (
        <Count
          value={imports}
          word="to import"
          className="text-read"
          testId="plan-import-count"
        />
      )}
      {moves === 0 ? null : (
        <Count
          value={moves}
          word="to move"
          className="text-read"
          testId="plan-move-count"
        />
      )}
      {forgets === 0 ? null : (
        <Count
          value={forgets}
          word="to forget"
          className="text-text-strong"
          testId="plan-forget-count"
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
