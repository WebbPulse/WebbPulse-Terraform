/** A run's plan: its counts, the resources it touches and the outputs it changes. */

import type { PlanChanges, RunPlan } from '../../api/runPlan';
import { OutputChangeList } from './OutputChangeList';
import { PlanSummaryLine } from './PlanSummaryLine';
import { ResourceChangeList } from './ResourceChangeList';

/** Props for {@link PlanView}. */
export interface PlanViewProps {
  plan: RunPlan;
  /** What the apply reported doing, once the run applied. */
  applyChanges?: PlanChanges | null | undefined;
  /** Whether the run destroyed everything, which the apply summary names as such. */
  isDestroy?: boolean | undefined;
}

/**
 * The parsed plan.
 *
 * The counts come first, then the resources, then the outputs, which is the
 * order a person reads a plan in: how much is changing, what is changing, and
 * what comes out the other side. Once the run applied, the apply's own summary
 * sits under the plan's and the outputs show their applied values.
 */
export function PlanView({
  plan,
  applyChanges,
  isDestroy = false,
}: PlanViewProps): React.ReactElement {
  const resourceChanges = plan.resource_changes ?? [];
  const outputChanges = plan.output_changes ?? [];
  return (
    <div data-testid="plan-view" className="space-y-5">
      <div className="space-y-2">
        <PlanSummaryLine plan={plan} />
        {plan.terraform_version === undefined ||
        plan.terraform_version === '' ? null : (
          <p className="text-xs text-text-faint">
            Planned with{' '}
            <span className="font-mono">{plan.terraform_version}</span>
          </p>
        )}
        {applyChanges === undefined || applyChanges === null ? null : (
          <p
            data-testid="apply-summary-line"
            className="font-mono text-sm text-text tabular-nums"
          >
            {isDestroy ? 'Destroy' : 'Apply'} complete! Resources:{' '}
            {applyChanges.add ?? 0} added, {applyChanges.change ?? 0} changed,{' '}
            {applyChanges.destroy ?? 0} destroyed.
          </p>
        )}
      </div>

      {plan.has_changes ? (
        <section
          aria-labelledby="resource-changes-heading"
          className="space-y-2"
        >
          <h3
            id="resource-changes-heading"
            className="text-sm font-semibold text-text-strong"
          >
            Resource changes
          </h3>
          <ResourceChangeList changes={resourceChanges} />
        </section>
      ) : null}

      {outputChanges.length === 0 ? null : (
        <section aria-labelledby="output-changes-heading" className="space-y-2">
          <h3
            id="output-changes-heading"
            className="text-sm font-semibold text-text-strong"
          >
            Outputs
          </h3>
          <OutputChangeList
            outputs={outputChanges}
            applied={plan.applied_outputs}
          />
        </section>
      )}
    </div>
  );
}
