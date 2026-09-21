/** The outputs a plan changes. */

import type { PlanOutputChange } from '../../api/runPlan';
import { actionGlyph, actionTone, formatPlanValue } from './planDiff';

/** Props for {@link OutputChangeList}. */
export interface OutputChangeListProps {
  outputs: readonly PlanOutputChange[];
}

/** The glyph colour for each action. */
const GLYPH_CLASSES: Record<string, string> = {
  add: 'text-add',
  change: 'text-change',
  destroy: 'text-destroy',
  replace: 'text-replace',
  read: 'text-read',
  none: 'text-text-faint',
};

/** The outputs the plan changes, or nothing when it changes none. */
export function OutputChangeList({
  outputs,
}: OutputChangeListProps): React.ReactElement {
  if (outputs.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
        This plan changes no outputs.
      </p>
    );
  }
  return (
    <dl
      aria-label="Output changes"
      data-testid="output-changes"
      className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-panel"
    >
      {outputs.map((output) => (
        <div
          key={output.name}
          data-testid="output-change"
          data-output={output.name}
          data-action={output.action}
          className="grid grid-cols-[1.75rem_minmax(0,11rem)_minmax(0,1fr)] gap-x-2 px-3 py-2 text-xs"
        >
          <span
            aria-hidden="true"
            className={`text-center font-mono font-semibold ${GLYPH_CLASSES[actionTone(output.action)] ?? 'text-text-faint'}`}
          >
            {actionGlyph(output.action)}
          </span>
          <dt className="min-w-0 truncate font-mono text-text-muted">
            {output.name}
          </dt>
          <dd className="min-w-0 font-mono break-all text-text">
            <OutputValue output={output} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** The output's value, or the words standing in for one it will not print. */
function OutputValue({
  output,
}: {
  output: PlanOutputChange;
}): React.ReactElement {
  if (output.sensitive) {
    return <span className="text-text-faint">(sensitive value)</span>;
  }
  if (output.after_unknown) {
    return <span className="text-text-faint">(known after apply)</span>;
  }
  return <span>{formatPlanValue(output.after)}</span>;
}
