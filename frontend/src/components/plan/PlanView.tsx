/** A run's plan: its counts, the resources it touches and the outputs it changes. */

import { useState } from 'react';

import {
  changedResources,
  unchangedResourceCount,
  type PlanChanges,
  type PlanResourceChange,
  type RunPlan,
} from '../../api/runPlan';
import { Button } from '../Button';
import { EmptyState } from '../EmptyState';
import { SegmentedControl, type Segment } from '../SegmentedControl';
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
  /** Whether the counts lead the view, or are rendered apart with {@link PlanSummary}. */
  showSummary?: boolean | undefined;
}

/** Props for {@link PlanSummary}. */
export type PlanSummaryProps = Pick<
  PlanViewProps,
  'plan' | 'applyChanges' | 'isDestroy'
>;

/**
 * The parsed plan.
 *
 * The counts come first, then the resources, then the outputs, which is the
 * order a person reads a plan in: how much is changing, what is changing, and
 * what comes out the other side. Once the run applied, the apply's own summary
 * sits under the plan's and the outputs show their applied values. The
 * resources open on only those that change, with every resource a click away.
 * A page that puts something between the counts and the resources renders
 * {@link PlanSummary} itself and turns `showSummary` off.
 */
export function PlanView({
  plan,
  applyChanges,
  isDestroy = false,
  showSummary = true,
}: PlanViewProps): React.ReactElement {
  const outputChanges = plan.output_changes ?? [];
  return (
    <div data-testid="plan-view" className="space-y-5">
      {showSummary ? (
        <PlanSummary
          plan={plan}
          applyChanges={applyChanges}
          isDestroy={isDestroy}
        />
      ) : null}

      <ResourceSection plan={plan} />

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

/**
 * The plan's counts, the engine that planned it and, once the run applied, the
 * apply's own summary: how much is changing, before what is changing.
 */
export function PlanSummary({
  plan,
  applyChanges,
  isDestroy = false,
}: PlanSummaryProps): React.ReactElement {
  return (
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
  );
}

/** Which resources the list shows. */
type ResourceFilter = 'changed' | 'all';

/** The plural or singular noun for a count of resources. */
function resources(count: number): string {
  return count === 1 ? 'resource' : 'resources';
}

/**
 * The plan's resources, filtered to the changed ones by default.
 *
 * A reviewer wants what changes first, so that is the opening view, the way the
 * hosted product's run page opens. The full list, unchanged resources
 * included, sits behind a segmented control whose labels carry both counts.
 * A plan over the backend's cap lists only changes, and the full view says how
 * many unchanged resources it cannot show.
 */
function ResourceSection({
  plan,
}: {
  plan: RunPlan;
}): React.ReactElement | null {
  const [filter, setFilter] = useState<ResourceFilter>('changed');
  const listed: readonly PlanResourceChange[] = plan.resource_changes ?? [];
  const changed = changedResources(plan);
  const unchanged = unchangedResourceCount(plan);
  const omitted = plan.unchanged_omitted ?? 0;
  const total = changed.length + unchanged;
  if (total === 0) {
    return null;
  }

  const segments: Segment<ResourceFilter>[] = [
    { id: 'changed', label: `Changed (${String(changed.length)})` },
    { id: 'all', label: `All (${String(total)})` },
  ];
  const shown = filter === 'changed' ? changed : listed;

  return (
    <section
      aria-labelledby="resource-changes-heading"
      data-testid="resource-section"
      data-filter={filter}
      className="space-y-2"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <h3
            id="resource-changes-heading"
            className="text-sm font-semibold text-text-strong"
          >
            Resources
          </h3>
          <p
            data-testid="resource-filter-caption"
            className="text-xs text-text-faint"
          >
            {filter === 'changed'
              ? unchanged === 0
                ? 'Every resource in this plan changes.'
                : `${String(unchanged)} unchanged ${resources(unchanged)} hidden`
              : `Showing all ${String(total)} ${resources(total)}`}
          </p>
        </div>
        <SegmentedControl<ResourceFilter>
          label="Which resources to show"
          segments={segments}
          value={filter}
          onChange={setFilter}
        />
      </div>

      {filter === 'changed' && changed.length === 0 ? (
        <EmptyState
          title="No resources change in this plan."
          hint={`All ${String(total)} ${resources(total)} already match the configuration.`}
          action={
            <Button
              size="sm"
              onClick={() => {
                setFilter('all');
              }}
            >
              Show all {total} {resources(total)}
            </Button>
          }
        />
      ) : (
        <ResourceChangeList changes={shown} />
      )}

      {filter === 'all' && omitted > 0 ? (
        <p data-testid="unchanged-omitted" className="text-xs text-text-faint">
          {omitted} unchanged {resources(omitted)} not listed. Plans over 500
          resources list only what changes.
        </p>
      ) : null}
    </section>
  );
}
