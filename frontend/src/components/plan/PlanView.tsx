/** A run's plan: its counts, the resources it touches and the outputs it changes. */

import type { RunPlan } from '../../api/runPlan';
import { OutputChangeList } from './OutputChangeList';
import { PlanSummaryLine } from './PlanSummaryLine';
import { ResourceChangeList } from './ResourceChangeList';

/** Props for {@link PlanView}. */
export interface PlanViewProps {
  plan: RunPlan;
}

/**
 * The parsed plan.
 *
 * The counts come first, then the resources, then the outputs, which is the
 * order a person reads a plan in: how much is changing, what is changing, and
 * what comes out the other side.
 */
export function PlanView({ plan }: PlanViewProps): React.ReactElement {
  return (
    <div data-testid="plan-view" className="space-y-5">
      <div className="space-y-2">
        <PlanSummaryLine plan={plan} />
        {plan.terraform_version === '' ? null : (
          <p className="text-xs text-text-faint">
            Planned with{' '}
            <span className="font-mono">{plan.terraform_version}</span>
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
          <ResourceChangeList changes={plan.resource_changes} />
        </section>
      ) : null}

      {plan.output_changes.length === 0 ? null : (
        <section aria-labelledby="output-changes-heading" className="space-y-2">
          <h3
            id="output-changes-heading"
            className="text-sm font-semibold text-text-strong"
          >
            Outputs
          </h3>
          <OutputChangeList outputs={plan.output_changes} />
        </section>
      )}
    </div>
  );
}
